"""Write, review and revise the implementation document and the code delivery."""
from __future__ import annotations

import copy
import difflib
import json

from .engine import ResearchBlocked, digest
from .independent_full_review import REVIEWERS
from .async_ownership import gather

STRATEGY = 'document_code'
KEY = 'implementation_work'
TEXT = {'type': 'string', 'minLength': 1}
FINDING = {'type': 'object', 'properties': {'location': TEXT, 'change': TEXT},
           'required': ['location','change'], 'additionalProperties': False}
VOTE = {'type': 'object', 'properties': {'approve': {'type':'boolean'}, 'rationale': TEXT,
        'findings': {'type':'array', 'items': FINDING}},
        'required': ['approve','rationale','findings'], 'additionalProperties': False,
        'anyOf':[{'properties':{'approve':{'const':True},'findings':{'maxItems':0}}},
                 {'properties':{'approve':{'const':False},'findings':{'minItems':1}}}]}


def active(state):
    return state.get('implementation_review_strategy') == STRATEGY


def wire_work(work):
    """Publish the scoped receipt fields separately from credential redaction."""
    materials = dict(work['materials'])
    def public(source):
        value = source.get('validation_row_identity')
        if not value:
            return source
        grant = value.get('authorization',value.get('grant'))
        keys = ('run_id','cycle','workflow_id','definition_id','proposal_sha256','review_handoffs',
                'plan_sha256','format','phase','purpose','files','sha256')
        if not isinstance(grant,dict) or set(grant)!=set(keys) or grant['sha256']!=digest({k:grant[k] for k in keys if k!='sha256'}):
            raise ResearchBlocked('无法认证投递给实现角色的行身份用途回执。')
        return {**source,'validation_row_identity':{'grant':{k:grant[k] for k in keys},'interface':value['interface']}}
    materials = public(materials)
    if 'asset_interface' in materials:
        materials['asset_interface'] = public(materials['asset_interface'])
    return {**work,'materials':materials}


def _context(engine, run_id, state, action, materials):
    config = engine.store.get('runs',run_id)['config']
    return {KEY:{'version':1, 'action':action, 'workflow_id':state['id'], 'cycle':state['cycle'],
                 'materials': materials}, 'implementation_acceptance_protocol':1,
            'acceptance_workflow_id':state['id'],
            'acceptance_budget':{'max_calls':config.get('implementation_review_max_calls',240),
                                 'max_seconds':config.get('implementation_review_max_seconds',14400)}}


def _stored(engine, run_id, role, handoff_id):
    from .implementation_alignment_evidence import _delivery
    decision = engine.store.get('decisions', handoff_id, {})
    result = decision.get('result')
    if not isinstance(result,dict):
        raise ResearchBlocked('缺少该实现请求的原始答复。')
    context = _delivery(engine,run_id,role,handoff_id,result)
    return result, context[KEY]


def check_votes(engine, run_id, votes, materials):
    identity = materials['identity']
    if len(votes) != len(REVIEWERS) or {v['role'] for v in votes} != set(REVIEWERS):
        raise ResearchBlocked('实现审核缺少同版本的独立意见。')
    frozen_work = None
    for vote in votes:
        answer, work = _stored(engine,run_id,vote['role'],vote['handoff_id'])
        if (work['materials'] != materials or work['action'] != identity['action']
                or work['workflow_id'] != identity['workflow_id'] or work['cycle'] != identity['cycle']
                or answer != vote['result'] or answer['approve'] == bool(answer['findings'])
                or frozen_work is not None and frozen_work != work):
            raise ResearchBlocked('审核意见、评审材料或版本不一致。')
        frozen_work = work


async def collect_reviews(collect, assignments):
    """Let admitted peers finish on a review error. A pause cancels the whole group."""
    async def settle(args):
        try:
            return await collect(*args)
        except Exception as exc:
            return exc
    results = await gather(*(settle(args) for args in assignments))
    for result in results:
        if isinstance(result,Exception):
            raise result
    return results


async def _review(engine, h, run_id, state, action, materials):
    identity = materials['identity']
    context = _context(engine,run_id,state,action,materials)
    purpose = action + ':' + digest(context[KEY])
    responsibility = ('文档审核判断这份说明是否足以写出正确代码。宿主已负责的内部检查按已交付接口使用，'
                      '源码实现和实测结果在写码之后审核。' if action=='implementation_doc_review' else
                      '代码审核核对实际交付的实现、接口和已有检查，报告影响正确性的具体问题。')
    task = (responsibility+'首版核对完整材料，修订版核对上一版意见和实际改动。'
            '按冻结协议判断。每个阻断项写明位置和应改内容。没有阻断项时同意并返回空 findings。'
            '宿主已核对版本和摘要。重点判断实现行为与授权范围，具体冲突指出对应材料。')
    schema=VOTE
    if 'validation_repair' in materials:
        task+=(' 本次票只判断修订后的代码能否在冻结计划和资源约束内运行 validation。'
               '核对上轮意见、实际差异、失败调用与计划参数，报告会阻断当前验证或导致结果错误的具体问题。'
               '对已有证据说明适用版本。当前真实 CLI 的运行结果由下一步实验产生。'
               '最终 test 的权限、评分、以后阶段的实验以及单独许可的夹具，请将未完成项写在 rationale 中，'
               '只有它们实际阻止本次 validation 时才列入 findings。批准范围随票冻结。')
    if 'implementation_history' in materials:
        task+=' implementation_history 给出本科研任务已登记的程序和本工作流批准交付。遇到兼容性问题，引用批准文档具体条款和对应的参照物，核对当前冻结参数控制、已登记程序或明确指定的外部参考。若确需外部原件，说明缺少哪件、依据和它阻断的具体操作，区分代码判断与正式执行许可。'
    if 'fixture_check' in materials and 'fixture_receipt' not in materials:
        schema=copy.deepcopy(VOTE)
        schema['properties']['fixture_check']={'type':'object','additionalProperties':False,
            'properties':{'sha256':{'const':materials['fixture_check']['sha256']},
                'approve':{'type':'boolean'},'reason':TEXT},'required':['sha256','approve','reason']}
        schema['required'].append('fixture_check')
        schema['if']={'properties':{'fixture_check':{'properties':{'approve':{'const':False}}}}}
        schema['then']={'properties':{'approve':{'const':False}}}
        task+=' 独立核对 fixture_check 的清单、完整探针和原批准义务；检查只使用许可的隔离 CPU 夹具、覆盖原 A/B 和所有负例、实际调用同版消费代码并采集真实断言，且不训练或触及正式资产。fixture_check.approve 仅表示允许宿主执行此确切探针，整份代码单独表态。'
    if 'fixture_receipt' in materials:
        if 'fixture_reuse' in materials:
            task+=' fixture_reuse 绑定前次已执行的隔离核验及当前逐字节相同的运行模块、配置、探针和清单。原回执保留其原始交付身份。本轮核对代码实际改动和余下问题。'
        else: task+=' 这是同版真实宿主夹具回执的复审，结合 fixture_receipt、原票和真实代码判断剩余问题。'
        if not materials['fixture_receipt']['passed']:
            schema=copy.deepcopy(VOTE)
            schema['properties']['approve']={'const':False}
    async def one(role):
        result,hid = await _ask(engine,h,run_id,role,purpose,task,context,schema)
        return {'role':role,'handoff_id':hid,'result':result}
    votes = await collect_reviews(one, [(role,) for role in REVIEWERS])
    check_votes(engine,run_id,votes,materials)
    return votes


async def _ask(engine,h,run_id,role,purpose,task,context,schema):
    from .codex_workflow import _frozen_spec_assignment
    frozen = _frozen_spec_assignment(engine,run_id,purpose,role)
    if frozen:
        # An author's ordinal is fixed by the controller. Reuse its original
        # host capability documentation across deployments and budget changes.
        prior_work = frozen['context'][KEY]
        if prior_work != context[KEY] and not (role=='engineer'
                and prior_work['workflow_id']==context[KEY]['workflow_id'] and prior_work['cycle']==context[KEY]['cycle']
                and prior_work['action']==context[KEY]['action']=='implementation_write'
                and all(prior_work['materials'].get(k)==context[KEY]['materials'].get(k) for k in
                    ('approved_base','base_spec_hash','previous','previous_reviews','experiment_plan','architecture_document',
                     'correction','document_revision','code_findings','approved_proposal','approved_design'))
                and prior_work['materials'].get('validation_row_identity',{}).get('authorization')==
                    context[KEY]['materials'].get('validation_row_identity',{}).get('authorization')):
            raise ResearchBlocked('该实现操作的冻结材料已变化，不能复用原请求。')
        task, context, schema = (frozen[k] for k in ('instruction','context','schema'))
    hid = h.decision_key(run_id,role,purpose,task,context,schema)
    return await h.ask(run_id,role,purpose,task,context,schema), hid


def _diff(before, after):
    left,right = before.splitlines(keepends=True),after.splitlines(keepends=True)
    return [{'previous_lines':[a,b],'current_lines':[c,d],
             'removed':''.join(left[a:b]),'added':''.join(right[c:d])}
            for op,a,b,c,d in difflib.SequenceMatcher(None,left,right,autojunk=False).get_opcodes() if op!='equal']


def _identity(state, action, **fields):
    return {'workflow_id':state['id'], 'cycle':state['cycle'], 'action':action,
            'proposal_hash':state['proposal_hash'], 'design_hash':digest(state['common']['design']),
            'experiment_plan_hash':digest(state['common']['experiment_plan']), **fields}


def prior_votes(engine,run_id,row):
    from .implementation_alignment_evidence import _delivery
    from . import independent_full_review as full
    reviews = []
    votes = row.get('definition_reviews') or row['review_votes']
    historical_three = False
    expected = ({k:row[k] for k in ('code_sha256','delivery_hash')} if row.get('delivery_hash') else
                {'spec_hash':row['spec_hash'],'document_sha256':row['document']['sha256']})
    for vote in votes:
        role = vote['role']
        if full.KEY in vote:
            full.authenticate(engine.store,run_id,vote)
            hid, result = vote[full.KEY]['handoff_id'], full.answer(vote)
            binding = result
        else:
            hid = vote['handoff_id']
            result = vote.get('result',{k:v for k,v in vote.items() if k not in ('role','handoff_id')})
            frozen = _delivery(engine,run_id,role,hid,result)
            binding = frozen[KEY]['materials']['identity'] if KEY in frozen else result
            historical_three |= KEY not in frozen and full.KEY not in frozen
        if any(binding.get(k)!=v for k,v in expected.items()):
            raise ResearchBlocked('上一轮意见与该次文档或代码版本不符。')
        reviews.append({'role':role,'handoff_id':hid, **{k:copy.deepcopy(result[k]) for k in
            ('approve','rationale','findings','issues','fixes','resolutions','host_asset_next_action',
             'asset_interface_proposal_review') if k in result}})
    roles = {'director','engineer','scout'} if historical_three else set(REVIEWERS)
    if len(reviews)!=len(roles) or {v['role'] for v in reviews}!=roles:
        raise ResearchBlocked('上一版未收齐独立评审意见。')
    return reviews


def _sources(engine, run_id, state, common):
    from .document_index import load
    from .experiment_materials import compile_view
    sources = {k:common[k] for k in ('dataset_catalog','execution_constraints','runtime_repair','prior_program',
               'dataset_adaptation','existing_implementation','definition_adoption_id') if k in common}
    for key in ('architecture_document','implementation_capability_document','existing_method_document','approved_definition_document'):
        if key in common:
            doc = common[key]
            _, text = load(engine,run_id,doc['document_id'],doc['sha256'])
            sources[key] = {'document_id':doc['document_id'],'sha256':doc['sha256'],'text':text}
    plan = state['common']['experiment_plan']
    sources['experiment_plan'] = compile_view(plan) or plan
    from . import dataset_row_identity
    grant = dataset_row_identity.workflow_grant(engine,run_id,state,
        dataset_row_identity.current(engine.store,run_id,state['cycle']))
    if grant:
        sources['validation_row_identity'] = {'authorization':grant,'interface':dataset_row_identity.interface()}
        sources['row_identity_host_sources'] = dataset_row_identity.handoff_sources()
    return sources


def _new_host_explanation(engine,run_id,state,row):
    """Re-review the same document when its host contract gains implementation evidence."""
    from . import dataset_row_identity as rows
    if not row or not active(row) or row.get('approved') is not False or row.get('host_explanation'):
        return None
    _,work = _stored(engine,run_id,'engineer',row['authoring_handoff_id'])
    old = work['materials'].get('validation_row_identity',{})
    if not old or rows.current(engine.store,run_id,state['cycle'])!=old['authorization']:
        return None
    previous,current = old['interface'],rows.interface()
    if (previous.get('host_handoff')==current['host_handoff']
            or {k:v for k,v in previous.items() if k!='host_handoff'}!={k:v for k,v in current.items() if k!='host_handoff'}):
        return None
    from .codex_workflow import _frozen_spec_author
    if _frozen_spec_author(engine,run_id,f"implementation_write:{state['id']}:{row['index']+1}"):
        return None
    return {'version':2,'host_handoff':current['host_handoff'],'source_ranges':rows.handoff_sources(),
            'grant_sha256':old['authorization']['sha256']}


async def document(engine,h,run_id,state,common,save):
    from . import implementation_document_revision as revisions
    from . import implementation_documents as docs
    from .codex_workflow import SPEC, _validate_spec
    from .implementation_failures import ImplementationSpecificationPending
    from .implementation_authoring_preservation import SaveConflict
    from .intelligence import ProviderError
    rows = state['specifications']
    prior = rows[-1] if rows else None
    from .implementation_cycle_transition import source as transition_source
    amendment = revisions.current(engine,run_id,state)
    transition = transition_source(engine,run_id,state) if not amendment else None
    explanation = _new_host_explanation(engine,run_id,state,prior)
    if explanation:
        prior_votes(engine,run_id,prior)
        prior['prior_review_round'] = {'approved':False,'votes':prior['review_votes']}
        prior.update(host_explanation=explanation,review_votes=[],approved=None)
        save(status='spec_review',current_revision=prior['index'])
    if prior is None or prior.get('approved') is False or amendment and prior['index']==amendment['base_index']:
        index = prior['index'] + 1 if prior else 0
        limit = engine.store.get('runs',run_id)['config'].get('implementation_review_max_revisions')
        first_index = amendment['base_index']+1 if amendment else transition['previous_index']+1 if transition else 0
        if limit is not None and index-first_index > limit:
            raise ResearchBlocked('实现文档修订达到当前配置上限。')
        materials = {**_sources(engine,run_id,state,common),'code_contract':common['code_contract'],
            'format_contract':common['implementation_format_contract']}
        if prior:
            materials.update(previous=prior['spec'], previous_reviews=prior_votes(engine,run_id,prior))
        repair = state.get('repair') or {}
        runtime_edit = repair.get('operator_instruction')
        if runtime_edit:
            parent = engine.store.get('codex_workflows',repair['program']['delivery_id'])
            approved = next(s for s in parent['specifications'] if s.get('approved') and s['spec_hash']==repair['program']['spec_hash'])
            materials.update(previous=prior['spec'] if prior else approved['spec'],
                             document_revision={'instruction':runtime_edit,'source_document':approved['document'],
                                                'trial_id':repair['trial_id'],'receipt_sha256':repair['receipt_sha256']})
        if amendment:
            from .storage import redact
            origin = next(r for r in rows if r['index']==amendment['base_index'])
            attempt = next(a for a in state['attempts'] if a['index']==amendment['attempt_index'])
            if (origin['spec_hash']!=amendment['spec_hash'] or origin['document']['sha256']!=amendment['document_sha256']
                    or attempt['delivery_hash']!=amendment['delivery_hash']
                    or [v['handoff_id'] for v in attempt['review_votes']]!=amendment['review_handoffs']):
                raise ResearchBlocked('文档更正所依据的原审核记录不一致。')
            materials.update(document_revision=amendment,code_findings=prior_votes(engine,run_id,attempt))
            for name in ('proposal','design'):
                source = state['common'][name]
                materials['approved_'+name] = {'source_sha256':digest(source),'content':redact(source)}
        if transition:
            from . import host_asset_definition_recovery as assets, implementation_definition_repair as definitions
            old = next(r for r in rows if r['index']==transition['previous_index'])
            evidence = await engine._read_consensus_check(run_id,assets.collect,engine,run_id,state,state[definitions.FIELD])
            if transition['asset_evidence_sha256'] and evidence is None:
                raise ResearchBlocked('原工程定义绑定的资产接口证据缺失。')
            if evidence:
                correction = await engine._read_consensus_check(run_id,assets.correction_needed,engine,run_id,state,
                    state[definitions.FIELD],old,evidence)
                if correction and prior['index']>transition['previous_index']:
                    _,last_work = _stored(engine,run_id,'engineer',prior['authoring_handoff_id'])
                    if last_work['materials'].get('asset_interface')==evidence:
                        raise ResearchBlocked('同一宿主接口证据的定向更正仍被退回，须核对新的能力或外部条件。')
                materials['asset_interface'] = evidence
            materials['original_definition_issues'] = definitions.issue_context(engine,state)
        task = ('依据批准的架构、实验计划和运行接口编写简明实现文档。覆盖数据划分、分支、参数、模块接口与验收。'
                '版本、摘要比对和授权记录由宿主维护。正文用持久的字段路径或文件位置说明实际用法。'
                'shared_context_records、shared_context_text 的编号只在单次传输内有效，不写入实现文档。')
        schema = SPEC
        from .experiment_configuration import DATA_READING_GUIDANCE
        if DATA_READING_GUIDANCE not in materials['code_contract']:
            task += '\n' + DATA_READING_GUIDANCE
        if amendment or runtime_edit:
            materials['base_spec_hash'] = digest(materials['previous'])
            schema = revisions.schema(materials['base_spec_hash'])
            task += ('按 document_revision.instruction 对 previous 和关联说明定点更正。'
                '使用已有字符串字段的 JSON Pointer，before 逐字复制该字段中唯一匹配的原文，after 提交替换文本。'
                '同步解决过时冲突登记和关联条款。保留授权范围外的正文、验收标准和真实划分。'
                '按本次授权处理冲突的旧条款。其他代码问题留给码工。')
        elif transition:
            base = definitions.base_row(state,state[definitions.FIELD])
            materials['approved_base'] = {'index':base['index'],'spec_hash':base['spec_hash']}
            schema = {'type':'object','additionalProperties':False,
                      'properties':{'addition':{**TEXT,'pattern':r'\S'}},'required':['addition']}
            task += '此版本接续已批准文档的工程补充。只返回替换上版退回补充段的新正文；宿主保留已批准正文。'
        elif prior:
            from .implementation_authoring import write_arguments_schema
            schema = write_arguments_schema(SPEC)['oneOf'][1]
            schema['properties'].pop('expected_sha256',None)
            schema['properties']['base_spec_hash'] = {'const':prior['spec_hash']}
            materials['base_spec_hash'] = prior['spec_hash']
            task += '按上一版双方意见返回针对 previous 的 JSON Pointer patch 与 base_spec_hash，宿主保存修订版。'
        context = _context(engine,run_id,state,'implementation_write',materials)
        purpose = 'implementation_write:'+state['id']+':'+str(index)
        result,hid = await _ask(engine,h,run_id,'engineer',purpose,task,context,schema)
        try:
            spec = authored_spec(state,materials,result)
            _validate_spec(spec)
        except (SaveConflict,ResearchBlocked,ProviderError) as exc:
            _stored(engine,run_id,'engineer',hid)
            corrected = {**materials,'correction':{'original_handoff_id':hid,'original_response':result,'error':str(exc)}}
            context = _context(engine,run_id,state,'implementation_write',corrected)
            result,hid = await _ask(engine,h,run_id,'engineer',purpose+':correction:'+digest(result),
                task+' 按 correction 中的保存错误修正本次提交。',context,schema)
            spec = authored_spec(state,materials,result)
            _validate_spec(spec)
        row = {'index':index,'spec':spec,'spec_hash':digest(spec),'authoring_handoff_id':hid,
               'implementation_review_strategy':STRATEGY,'review_material_version':2}
        if runtime_edit:
            from .validation_repair import invocation
            row['repair_invocation'] = invocation(engine,run_id,repair)
        docs.save_specification(engine,h,run_id,state,row,common)
        rows.append(row)
        save(status='spec_review',current_revision=index)
    else:
        row = prior
    if row.get('approved') is True:
        return row
    materials = document_materials(engine,run_id,state,row)
    votes = await _review(engine,h,run_id,state,materials['identity']['action'],materials)
    row.update(review_votes=votes,approved=all(v['result']['approve'] for v in votes))
    save(status='implementing' if row['approved'] else 'specification',
         current_revision=row['index'] if row['approved'] else row['index']+1)
    if not row['approved']:
        raise ImplementationSpecificationPending()
    return row


def authored_spec(state, materials, result):
    if 'approved_base' in materials:
        from . import implementation_definition_repair as definitions
        base = definitions.base_row(state,state[definitions.FIELD])
        if materials['approved_base'] != {'index':base['index'],'spec_hash':base['spec_hash']}:
            raise ResearchBlocked('工程补充引用了其他批准版本。')
        spec = {**base['spec'],'algorithm':base['spec']['algorithm']+'\n\n'+result['addition']}
        definitions.validate_candidate(spec,state,definitions.binding(state))
        return spec
    if 'base_spec_hash' in materials:
        from .implementation_authoring_preservation import _patch
        if result.get('base_spec_hash')!=digest(materials['previous']):
            raise ResearchBlocked('文档修改没有绑定原版本。')
        if 'document_revision' in materials:
            from .implementation_document_revision import apply
            return apply(materials['previous'],result['edits'])
        return _patch(materials['previous'],result['patch'])
    return result


def document_materials(engine,run_id,state,row):
    from .document_index import load
    _,work = _stored(engine,run_id,'engineer',row['authoring_handoff_id'])
    sources = work['materials']
    identity = _identity(state,'implementation_doc_review',spec_hash=row['spec_hash'],
                         document_sha256=row['document']['sha256'])
    _,body = load(engine,run_id,row['document']['document_id'],row['document']['sha256'])
    materials = {'identity':identity,'implementation_document':body, **{k:v for k,v in sources.items()
        if k not in ('previous','previous_reviews','code_contract','format_contract','base_spec_hash','correction')}}
    previous = next((r for r in reversed(state['specifications']) if r['index'] < row['index']),None)
    if 'repair_invocation' in row:
        materials['repair_invocation'] = row['repair_invocation']
    if not previous and 'repair_invocation' in row and 'source_document' in sources.get('document_revision', {}):
        doc = sources['document_revision']['source_document']
        _,before = load(engine,run_id,doc['document_id'],doc['sha256'])
        materials.update(previous_spec_hash=sources['base_spec_hash'],changes=_diff(before,body))
    if previous:
        _,before = load(engine,run_id,previous['document']['document_id'],previous['document']['sha256'])
        materials.update(previous_reviews=prior_votes(engine,run_id,previous),previous_spec_hash=previous['spec_hash'],
                         changes=_diff(before,body))
        if row.get('review_material_version')==2 and active(previous):
            # Both reviewers already received the same scientific sources for
            # this document. Keep changed sources, the actual current document
            # and the full diff; identify unchanged inputs by their exact version.
            _,prior_work = _stored(engine,run_id,'engineer',previous['authoring_handoff_id'])
            unchanged = {}
            for key in ('architecture_document','experiment_plan','existing_method_document','approved_definition_document'):
                if key in materials and materials[key]==prior_work['materials'].get(key):
                    value = materials.pop(key)
                    unchanged[key] = ({'document_id':value['document_id'],'sha256':value['sha256']}
                                      if 'document_id' in value else {'sha256':digest(value)})
            materials['unchanged_sources'] = unchanged
    if row.get('host_explanation'):
        previous_round = {**row,'review_votes':row['prior_review_round']['votes']}
        materials['host_explanation'] = row['host_explanation']
        materials['previous_reviews'] = prior_votes(engine,run_id,previous_round)
        if row['host_explanation'].get('version')==2:
            materials.update(previous_spec_hash=row['spec_hash'],changes=[])
        materials['review_scope'] = ('同一份已保存文档，新增宿主函数链和实际源码范围。'
            '用它们核对上一轮具体疑问。宿主负责其实现与部署，作者负责适配器的行为。'
            '代码作者也会收到这份说明。请独立判断两者合并后是否足以实现。')
    return materials


def validate_document(engine,run_id,state,row):
    if state.get('pre_codex_spec_source') and row==state['specifications'][0]:
        from .pre_codex_restart import inherited_spec
        source = inherited_spec(engine,run_id,state['id'])
        if source and source[0]['id']==state['pre_codex_spec_source'] and source[1]==row:
            return
        raise ResearchBlocked('继承文档与已审核的恢复来源不一致。')
    result, work = _stored(engine,run_id,'engineer',row['authoring_handoff_id'])
    if (work['action'] != 'implementation_write' or work['workflow_id'] != state['id']
            or authored_spec(state,work['materials'],result) != row['spec']
            or digest(row['spec']) != row['spec_hash'] or row.get('approved') is not True):
        raise ResearchBlocked('已保存文档与原作者交付不一致。')
    check_votes(engine,run_id,row['review_votes'],document_materials(engine,run_id,state,row))
    if not all(v['result']['approve'] for v in row['review_votes']):
        raise ResearchBlocked('当前文档仍有未解决的审核意见。')


def author_materials(engine,run_id,specification):
    if active(specification):
        return _stored(engine,run_id,'engineer',specification['authoring_handoff_id'])[1]['materials']
    return {}


def implementation_history(engine,run_id,state,specification):
    previous=state['common'].get('previous_cycle') or {}
    program=engine.store.get('programs',run_id) or {}
    return {'version':1,'research_cycle':state['cycle'],'previous_cycle_id':previous.get('id'),
            'workflow_id':state['id'],'document_sha256':specification['document']['sha256'],
            'reference_policy':'实现自拟用例按已批准文档规定的对象适用。文档只指定当前冻结参数控制且无已指定的历史版本时，将历史版本核验单列为本轮不适用。若批准材料明确指定历史参照，绑定原件后核对，对缺件保留未核验及对应操作限制。',
            'registered_program':{k:program[k] for k in ('id','revision','delivery_id','delivery_hash','code_sha256')
                                  if k in program} or None,
            'prior_approved_deliveries':[a['delivery_hash'] for a in state['attempts']
                                         if a['status']=='approved' and a.get('delivery_hash')]}


def code_materials(engine,run_id,state,specification,attempt):
    from .code_revision_review import _directory, _changes
    from .document_index import load
    identity = _identity(state,'implementation_code_review',spec_hash=specification['spec_hash'],
            document_sha256=specification['document']['sha256'],code_sha256=attempt['code_sha256'],
            delivery_hash=attempt['delivery_hash'])
    prior = next((a for a in reversed(state['attempts']) if a['index'] < attempt['index']
                  and a.get('review_votes')),None)
    parent_docs = []
    if not prior and attempt.get('reviewed_parent_delivery'):
        program = state['repair']['program']
        parent = engine.store.get('codex_workflows',program['delivery_id'])
        if parent['result']['delivery_hash'] != attempt['reviewed_parent_delivery']:
            raise ResearchBlocked('运行修订与已审核的父交付不一致。')
        prior = next(a for a in parent['attempts'] if a.get('status')=='approved'
                     and a['delivery_hash']==attempt['reviewed_parent_delivery'])
        parent_docs = parent['specifications']
    before = _directory(engine,run_id,prior) if prior else {}
    after = _directory(engine,run_id,attempt)
    changes = _changes(engine,run_id,before,after)
    doc = specification['document']
    _,body = load(engine,run_id,doc['document_id'],doc['sha256'])
    materials = {'identity':identity,'implementation_document':body,'files':changes,
        'local_checks':{k:v for k,v in attempt['acceptance'].items()
                        if k in ('approved','issues','checks','scope','resource_admission')}}
    if 'implementation_history' in attempt: materials['implementation_history']=attempt['implementation_history']
    if 'repair_invocation' in attempt: materials['repair_invocation']=attempt['repair_invocation']
    if 'validation_repair' in attempt: materials['validation_repair']=attempt['validation_repair']
    if attempt.get('development_check_evidence'):
        from .implementation_check_evidence import verified
        materials['development_check_evidence']=verified(engine,run_id,attempt)
    if specification.get('host_explanation'):
        materials['host_explanation'] = specification['host_explanation']
    if attempt.get('review_material_version') in (2,3) or not specification.get('host_explanation'):
        authored = author_materials(engine,run_id,specification)
        if 'row_identity_host_sources' in authored:
            materials['row_identity_host_sources'] = authored['row_identity_host_sources']
        if attempt.get('review_material_version') in (2,3):
            if 'validation_row_identity' in authored:
                materials['validation_row_identity'] = authored['validation_row_identity']
            if 'host_checks' in attempt['acceptance']:
                materials['local_checks']['host_checks'] = attempt['acceptance']['host_checks']
    if prior:
        materials.update(previous_reviews=prior_votes(engine,run_id,prior),previous_delivery=prior['delivery_hash'])
        if attempt.get('review_material_version')==3:
            from .implementation_alignment_evidence import _code_text
            manifest = before['implementation.json']
            prior_sha = json.loads(_code_text(engine,run_id,manifest['artifact_id'],manifest)).get('implementation_document_sha256')
            if prior_sha != doc['sha256']:
                if not parent_docs:
                    materials['current_delivery'] = _changes(engine,run_id,{},after)
                materials['previous_document_sha256'] = prior_sha
                materials['review_scope'] = '批准文档已换版。按当前文档审核完整交付，并结合两份原意见及实际代码差异复核。'
                if parent_docs:
                    materials['review_scope'] = '核对本次运行修订的要求、文档和代码差异，以及改动直接影响的接口。结合原审核意见判断修正是否充分。'
                ancestor = next((r['document'] for r in [*state['specifications'],*parent_docs] if prior_sha
                    and r.get('document',{}).get('sha256')==prior_sha),None)
                if not ancestor:
                    raise ResearchBlocked('上一批交付绑定的原实现文档无法定位，不能编制换版审核材料。')
                _,old_body = load(engine,run_id,ancestor['document_id'],prior_sha)
                materials['document_changes'] = _diff(old_body,body)
    if attempt.get('fixture_grant_sha256'):
        from . import implementation_fixture as fixture
        packet=fixture.declaration(engine,run_id,state,specification,attempt)
        if not packet: raise ResearchBlocked('当前交付缺少已许可夹具的真实清单。')
        materials['fixture_check']=packet
        if attempt.get('fixture_reuse'):
            reused=fixture.matching_receipt(engine,run_id,state,specification,attempt,packet)
            if not reused or reused[0]!=attempt['fixture_reuse']:
                raise ResearchBlocked('复用的隔离核验与当前可执行输入或原始回执不一致。')
            materials.update(fixture_reuse=reused[0],fixture_receipt=reused[1])
        if attempt.get('fixture_receipt_sha256'):
            votes=attempt['fixture_admission_votes']
            check_votes(engine,run_id,votes,materials)
            if not all(v['result']['fixture_check']['approve'] and
                       v['result']['fixture_check']['sha256']==packet['sha256'] for v in votes):
                raise ResearchBlocked('夹具执行缺少确切版本的两份独立意见。')
            receipt=fixture.read_receipt(engine,run_id,packet)
            if not receipt or receipt['sha256']!=attempt['fixture_receipt_sha256']:
                raise ResearchBlocked('交付所绑定的夹具执行回执不一致。')
            materials.update(fixture_receipt=receipt,fixture_admission_votes=votes)
            if attempt.get('development_check_protocol')==1:
                # Both reviewers saw exactly these bytes in the preceding review.
                materials={k:materials[k] for k in ('identity','implementation_history','development_check_evidence',
                           'fixture_receipt','fixture_admission_votes') if k in materials}
                materials.update(review_scope='同一文档与同一代码快照的执行复核；原始代码审查见两份签名交接。代码改动为空。核对原意见与本次实测原件。',
                    approved_document_sections={k:specification['spec'][k] for k in ('acceptance_checks','parameter_contract')},
                    requirements=packet['grant']['requirements'],fixture_observations=fixture.observations(engine,run_id,packet,receipt))
            if attempt.get('fixture_failure_context'):
                materials['fixture_failure_context']=attempt['fixture_failure_context']
    return materials


def pending_fixture_evidence(engine,run_id,state,specification,attempt,intelligence):
    """Enrich a new review or a failed final-only handoff, retaining its original vote history."""
    if not attempt.get('fixture_receipt_sha256') or attempt.get('review_votes') or attempt.get('fixture_failure_context'):
        return None
    from . import implementation_fixture as fixture
    evidence=fixture.failure_context(engine,run_id,attempt)
    if not evidence: return None
    before=code_materials(engine,run_id,state,specification,attempt)
    action=before['identity']['action']
    purpose=action+':'+digest(_context(engine,run_id,state,action,before)[KEY])
    from .codex_workflow import _frozen_spec_assignment
    from .schema_recovery import derive_source
    files={(f['path'],f['sha256']) for k,f in evidence.items() if k.endswith('_file')}
    for role in REVIEWERS:
        saved=_frozen_spec_assignment(engine,run_id,purpose,role)
        if saved:
            progress=engine.store.get('handoff_progress',saved['id'],{})
            final=progress.get('finalization',{})
            if (progress.get('mode')!='final_only' or final.get('status')!='blocked'
                    or not derive_source(engine.store,intelligence,run_id,saved['id'],final,fixture_archives=files)):
                return None
    return evidence


async def code_review(engine,h,run_id,state,specification,attempt):
    materials = await engine._read_consensus_check(run_id,code_materials,engine,run_id,state,specification,attempt)
    return await _review(engine,h,run_id,state,materials['identity']['action'],materials)
