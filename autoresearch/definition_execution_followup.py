"""Continue approved definition work through evidence, without another old suite."""
from __future__ import annotations

import copy
from pathlib import Path

from . import code_repair_contract as repair, code_repair_clarification as clarification
from . import hrp_host_timing as host
from .storage import now


ACCEPTANCE_GUIDANCE = '''
definition_execution_acceptance 明确本次批准对象：已批准补充条款的代码落实、原截止缺陷的差异验证，
以及已实际完成的一次计时；不是训练许可、资源准入或全项目重新验收。
批准补充“汇总、峰值与验收”第5项明确允许必要依据不足时保持null/false/completed=0。
独立判断当前代码是否正确实现这一行为：若实现符合文档且本轮必要代码断言都有真实依据，
资源结果仍未通过本身不是代码缺陷，不应要求Codex把missing改成通过。
反之，具体实现偏差、原必要代码断言未执行或证据不支持本轮验收，必须继续拒绝并精确指出。
本轮approve=true仅批准上述代码符合性范围；原资源missing、旧timeout和正式训练门禁仍全部保留。
原三方意见均保留，可从prior_reviews恢复本人已确认的代码/文档依据，不重读未变化的已闭合项。
只澄清原issue_id，不新增资源方法、测量次数、科学要求或放宽原断言。无需重复计时或训练。
请明确判断而非为结束流程而批准；任何真实代码问题仍按批准文档提交最小修改。
'''


def acceptance_scope(attempt, record):
    return {'version':1,'kind':'approved_definition_code_conformance',
        'snapshot_sha256':attempt['snapshot']['files_hash'],
        'host_receipt_sha256':record['host_verification']['sha256'],
        'resource_admission_authorized':False,'training_authorized':False,
        'prior_reviews':[{k:v[k] for k in ('role','handoff_id')} for v in record.get('votes',[])]}


def validate_acceptance_scope(scope, context):
    host_evidence=context.get('host_verification',{})
    definition=context.get('definition_code_followup',{})
    if (scope.get('version')!=1 or scope.get('kind')!='approved_definition_code_conformance'
            or scope.get('snapshot_sha256')!=definition.get('snapshot_sha256')
            or scope.get('host_receipt_sha256')!=host_evidence.get('sha256')
            or host_evidence.get('identity',{}).get('kind')!='definition_execution_evidence_v1'
            or host_evidence.get('passed') is not True
            or scope.get('resource_admission_authorized') is not False
            or scope.get('training_authorized') is not False):
        raise ValueError('Code conformance scope requires completed, source-bound evidence without execution authority')


async def prepare(engine,h,run_id,state,spec,attempt,source,save):
    from .codex_workflow import _reviews, _frozen_spec_assignment
    from .implementation_alignment_evidence import validate_vote
    cached=attempt.get('verification_followup')
    if cached and cached.get('status')!='completed' and cached.get('acceptance_scope'):
        resolved,mapped,record=await clarification.clarify(engine,h,run_id,state,spec,attempt,source,
            clarification.mapping(source),save,host_verification=cached['host_verification'],
            record_key='verification_followup',acceptance_scope=cached['acceptance_scope'])
        await finalize(engine,run_id,spec,attempt,resolved,record,save)
        return resolved,mapped,record
    previous_plan=attempt.get('timing_entry_review',{}).get('host_verification',{}).get('identity',{})
    if (cached and cached.get('status')!='completed' and previous_plan.get('adapter_sha256')
            and previous_plan['adapter_sha256']!=host.file_sha(host.__file__)):
        attempt.setdefault('verification_followup_history',[]).append(copy.deepcopy(cached))
        attempt.pop('verification_followup');save();cached=None
    if cached and cached.get('status')=='completed':
        old=cached.get('host_verification',{}).get('identity',{})
        if (old.get('kind') in {'hrp_host_transport_plan_v1', 'supplemental_transport_plan_v2'}
                and old.get('adapter_sha256')!=host.file_sha(host.__file__)):
            # The old transport review stays immutable. A corrected host
            # adapter is fresh evidence, not another training/test allowance.
            await engine._read_consensus_check(run_id,clarification.verify,
                engine,run_id,state,spec,attempt,source,cached)
            attempt.setdefault('verification_followup_history',[]).append(copy.deepcopy(cached))
            attempt.pop('verification_followup')
            save()
            cached=None
    if cached and cached.get('status')=='completed':
        resolved=await engine._read_consensus_check(run_id,clarification.verify,
            engine,run_id,state,spec,attempt,source,cached)
        if (not cached.get('acceptance_scope')
                and cached.get('host_verification',{}).get('identity',{}).get('kind')=='definition_execution_evidence_v1'
                and cached['host_verification'].get('passed') is True
                and not any(i['binding']['disposition'] in {'repair_code','needs_definition','spec_conflict'}
                            for i in resolved['issues'])):
            # Retain the original rejection. Ask for an explicit code decision;
            # never convert an empty issue list or a timing success into approval.
            attempt.setdefault('verification_followup_history',[]).append(copy.deepcopy(cached))
            scope=acceptance_scope(attempt,cached)
            host_evidence=cached['host_verification']
            attempt.pop('verification_followup');save(status='code_review',blocked_kind=None,reason=None)
            resolved,mapped,cached=await clarification.clarify(engine,h,run_id,state,spec,attempt,source,
                clarification.mapping(source),save,host_verification=host_evidence,
                record_key='verification_followup',acceptance_scope=scope)
        await finalize(engine,run_id,spec,attempt,resolved,cached,save)
        return resolved,clarification.mapping(resolved),cached
    plan=await asyncio_thread_plan(engine,run_id,state,spec,attempt)
    proposal=clarification.mapping(source)
    entry_votes=copy.deepcopy(attempt['review_votes'])
    pending=[v['role'] for v in entry_votes if not v.get('timing_entry_approved')]
    if pending:
        context=clarification.context_for(source,proposal,host_verification=host.view(plan,attempt))
        # Entry admission needs the unresolved transport question only. The
        # deadline and actual timing receipts are reviewed after execution.
        questions=[i for i in context[repair.FIELD]['issues'] if i['role'] in pending
                   and i['binding']['disposition']=='needs_review' and i['requirement_id']=='C05']
        if questions:
            context[repair.FIELD]['issues']=questions
            context['implementation_alignment_acceptance_ids']=['C05']
        previous=attempt.get('timing_entry_review',{})
        if previous.get('votes'):
            context[repair.FIELD]['document_memory_handoffs'] += [
                {k:v[k] for k in ('role','handoff_id')} for v in previous['votes']]
        context['host_timing_entry_review']={
            'scope':'Only the outstanding C05 timing entry transport question. C06 and actual measured numbers '
                'remain preserved for the post-execution review; do not re-review them at entry admission.',
            'instruction':'宿主已提供真实输出捕获适配器、源码工件和截止差异单测。补查尚未确认的捕获边界；'
                '计时数字仍待宿主执行，不能要求先有数字才批准入口。原代码无变化，不重开已确认算子。'}
        purpose='code_review:'+repair.sha([state['id'],source['delivery_hash'],plan['sha256']])+':timing-entry'
        attempt['execution_progress']={'step':'timing_entry_review','label':'补查计时入口与宿主输出捕获边界',
                                       'started_at':now(),'updated_at':now()}
        save(status='code_review',blocked_kind=None,reason=None)
        votes=await _reviews(engine,h,run_id,purpose,context,delivery=True,roles=tuple(pending))
        # Authenticate every reused approval as well as the new role's approval.
        if previous and previous.get('purpose')!=purpose:
            attempt.setdefault('timing_entry_review_history',[]).append(copy.deepcopy(previous))
        attempt['timing_entry_review']={'purpose':purpose,'votes':votes,'host_verification':host.view(plan,attempt)}
        save()
        entry_votes=[next((n for n in votes if n['role']==v['role']),v) for v in entry_votes]
    for vote in entry_votes:
        new=attempt.get('timing_entry_review',{})
        purpose=new['purpose'] if vote in new.get('votes',[]) else attempt['review_purpose']
        frozen=_frozen_spec_assignment(engine,run_id,purpose,vote['role'])
        if not frozen or frozen['context']['delivery_hash']!=attempt['delivery_hash']:
            raise ValueError('Timing entry approval delivery mismatch')
        await engine._read_consensus_check(run_id,validate_vote,engine,run_id,vote['role'],vote['handoff_id'],
            {k:v for k,v in vote.items() if k not in {'role','handoff_id'}},frozen['context'],delivery=True)
    if not plan['passed'] or not all(v.get('timing_entry_approved') is True for v in entry_votes):
        # The original roles classify actual defects; transport metadata is not edit authority.
        return await clarification.clarify(engine,h,run_id,state,spec,attempt,source,proposal,save,
            host_verification=host.view(plan,attempt),record_key='verification_followup')
    attempt['execution_progress']={'step':'supplemental_timing','label':'执行 v48 一次补充计时；不限时、不训练、不重跑原探针',
                                   'started_at':now(),'updated_at':now()}
    save(status='acceptance',blocked_kind=None,reason=None)
    receipt=await host.run(engine,run_id,state,spec,attempt,plan,entry_votes)
    attempt['supplemental_timing_receipt_id']=receipt['id'];save()
    combined_identity={'kind':'definition_execution_evidence_v1',
        'snapshot_sha256':attempt['snapshot']['files_hash'],'plan_sha256':plan['sha256'],'timing_sha256':receipt['sha256']}
    combined=engine.store.get(host.RECEIPTS,run_id+':'+repair.sha(combined_identity))
    if not combined:
        combined=host.seal(engine,run_id,combined_identity,{
            'status':receipt['status'],'passed':plan['passed'] and receipt['passed'],
            'summary':{'plan_receipt_id':plan['id'],'timing_receipt_id':receipt['id'],
                       'scientific_approval':False,'resource_passed':False,
                       'timing_entry_review_handoffs':[{k:v[k] for k in ('role','handoff_id')} for v in entry_votes]},
            'test_reports':{**plan['test_reports'],**receipt.get('test_reports',{})},
            'transport':receipt.get('transport',{}),'error':receipt.get('error'),
            'limitations':receipt.get('limitations',[])},
            [a['path'] for a in [*plan['artifacts'],*receipt['artifacts']]])
    resolved,mapped,record=await clarification.clarify(engine,h,run_id,state,spec,attempt,source,proposal,save,
        host_verification=host.view(combined,attempt),record_key='verification_followup')
    await finalize(engine,run_id,spec,attempt,resolved,record,save)
    return resolved,mapped,record


async def finalize(engine,run_id,spec,attempt,resolved,record,save):
    from .codex_delivery import inspect_delivery
    votes=record.get('votes',[])
    if (not resolved['issues'] and len(votes)==len(repair.ROLES)
            and {v.get('role') for v in votes}==set(repair.ROLES)
            and all(v.get('approve') is True for v in votes)):
        if record.get('acceptance_scope'):
            covered={f['requirement_id'] for vote in record['votes'] for f in vote.get('alignment_findings',[])}
            if not {'C05','C06'}<=covered:
                raise ValueError('Scoped reviewers must jointly close both frozen C05/C06 obligations')
        combined=host.validate(engine,run_id,attempt,record['host_verification'])
        if combined['identity'].get('kind')!='definition_execution_evidence_v1':
            raise ValueError('A transport plan cannot substitute for actual execution')
        inspected=await engine._read_consensus_check(run_id,inspect_delivery,Path(attempt['snapshot']['workspace']),spec['spec'])
        if inspected['approved'] and combined['passed'] and inspected['delivery_hash']==attempt['delivery_hash']:
            attempt['acceptance'].update(approved=True,issues=[],definition_checks_deferred=False,
                host_verification=host.view(combined,attempt),scope='v48 定向代码验收与一次补充计时；不是训练或科研验收')
            if record.get('acceptance_scope'):
                attempt['acceptance']['code_conformance_only']=True
                attempt['acceptance']['resource_admission']={'approved':False,
                    'host_receipt_id':combined['id'],'host_receipt_sha256':combined['sha256'],
                    'missing':copy.deepcopy(combined.get('test_reports',{}).get('supplemental_timing',{}).get('missing',[])),
                    'training_authorized':False}
            save()


async def asyncio_thread_plan(engine,run_id,state,spec,attempt):
    return await engine._read_consensus_check(run_id,host.plan,engine,run_id,state,spec,attempt)
