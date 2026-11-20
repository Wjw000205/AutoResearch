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


