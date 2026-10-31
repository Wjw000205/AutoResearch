"""Scoped code review after an independently approved definition amendment.

Old whole-delivery requests remain immutable. A distinct purpose prevents their
cached votes from being mistaken for a review of this narrower execution step.
"""
from __future__ import annotations

import copy
from . import implementation_definition_repair as definitions
from . import code_repair_contract as repair
from .architecture_validation import operation

FIELD = 'definition_code_followup'
VERSION = 1

TASK = ('独立复核当前交付对已批准资源补充条款的落实。按本轮指定范围核对实际文件与原文，'
    '精确回传proposal_hash/spec_hash/code_sha256/delivery_hash。中文issues/fixes逐项关联，'
    'approve=true时issues/fixes均为空。作者完成声明不能代替实际代码或执行回执。')

GUIDANCE = '''本轮仅复核已批准资源估算补充的代码落实及原C05/C06未结问题。
implementation_alignment_acceptance_ids 指明本轮范围，不重开C01–C04及已完成科学定义，
不把C06这个大类理解为重读全部T01–T09；只查补充段所列算子、隔离计时入口、计账/覆盖判定，
以及本轮已保存的tests/test_delivery.py宿主截止时间覆盖问题和实际修改引入的相关回归。
先定位修改文件和具体函数，按需读当前实现文档补充段及原截止规则；不得让Codex自创要求。
定义已经三方批准，不再要求先取得新计时数字才能批准计时代码。timing_entry_approved仅指
独立非训练计时入口可交宿主执行，不代表完整实现、资源门禁或科学验收通过。
无执行回执用needs_execution，真实代码偏差用repair_code；缺必要定义用needs_definition。
每条issues用repair_findings精确绑定C05/C06、code_evidence和当前文档位置；不要复述全项目未审清单。
已有超时回执仍为失败，不重跑旧整套测试、不重置原共同60秒，不据exit_code=0判通过。
新增计时按已批准补充单独一次执行、无墙钟上限；执行前须三方确认入口符合补充。
旧报告只证明旧版本已完成的断言，不冒充当前改动已通过；当前必要差异核验明确列出即可。
先完成本轮具体判断；无需重新审查未改算法、论文来源和无关阶段。'''


def applicable(state, specification):
    record = state.get(definitions.FIELD,{})
    return (record.get('status')=='approved' and record.get('timing_authorization_id')
            and record.get('approved_index')==specification.get('index')
            and {'C05','C06'} <= {r['id'] for r in specification.get('spec',{}).get('acceptance_checks',[])})


@operation
def attach(engine, run_id, state, specification, attempt, context):
    if not applicable(state,specification): return context
    definitions.validate_release(engine,run_id,state,specification)
    base = next(a for a in state['attempts'] if a['index']==state[definitions.FIELD]['attempt_index'])
    before = {f['path']:f['sha256'] for f in base['snapshot']['files']}
    after = {f['path']:f['sha256'] for f in attempt['snapshot']['files']}
    result = copy.deepcopy(context)
    result[FIELD] = {'version':VERSION,'workflow_id':state['id'],
        'repair_id':state[definitions.FIELD]['id'],'spec_hash':specification['spec_hash'],
        'document_sha256':specification['document']['sha256'],
        'snapshot_sha256':attempt['snapshot']['files_hash'],
        'definition_addition':definitions.addition_ref(engine,run_id,state,specification),
        'timing_authorization':definitions.timing_authorization(engine,state),
        'changed_paths':sorted(p for p in before.keys() | after.keys() if before.get(p)!=after.get(p)),
        'full_acceptance_ids':[r['id'] for r in specification['spec']['acceptance_checks']],
        'requirement_ids':['C05','C06'],'scientific_approval':False,
        'review_scope':'approved resource definition, pending resource evidence and host deadline propagation'}
    result['implementation_alignment_acceptance_ids']=['C05','C06']
    result[FIELD]['document_memory_handoffs'] = [
        {k:v[k] for k in ('role','handoff_id')} for v in specification.get('review_votes',[])]
    return result


def review_schema(schema):
    result = repair.review_schema(schema)
    result['properties']['timing_entry_approved']={'type':'boolean'}
    if 'timing_entry_approved' not in result['required']:
        result['required'].append('timing_entry_approved')
    return result


def validate_review(value, context):
    scope = context.get(FIELD)
    if not scope: return
    if (scope.get('version')!=VERSION or scope['spec_hash']!=context['spec_hash']
            or scope['document_sha256']!=context['implementation_document']['sha256']):
        repair.fail('资源补充代码复审没有绑定当前批准版本。')
    findings = value.get('repair_findings',[])
    evidence = value.get('code_evidence',[])
    if (type(value.get('timing_entry_approved')) is not bool
            or len(findings)!=len(value['issues'])
            or {r['issue_index'] for r in findings}!=set(range(len(value['issues'])))
            or any(f['requirement_id'] not in scope['requirement_ids'] for f in value['alignment_findings'])):
        repair.fail('资源补充复审必须逐项分类，不能重开无关验收项。')
    for finding in findings:
        if (finding['requirement_id'] not in scope['requirement_ids']
                or not finding['evidence_indices']
                or any(type(i) is not int or not 0<=i<len(evidence)
                       or evidence[i]['requirement_id']!=finding['requirement_id'] for i in finding['evidence_indices'])):
            repair.fail('资源补充代码意见缺少同条款实际源码依据。')


def purpose(state, attempt):
    return f'code_review:{repair.sha(state["id"])}:{attempt["index"]}:definition-followup-v1'
