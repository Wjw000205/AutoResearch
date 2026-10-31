"""Route a proved, unchanged execution gap to review, never another blind run.

An exact failed host check may inform one independent review of the same
runtime. It cannot become a passed acceptance or a new experiment receipt.
Native turns, source snapshots, original votes and approved SPEC stay intact.
"""
from __future__ import annotations

import copy
from pathlib import Path

from . import code_repair_contract as repair

FIELD = 'execution_failure_review'

GUIDANCE = '''\n本轮是实际执行失败后的定向复审，不是通过验收。execution_failure_review 绑定原宿主失败、
原生交付及同一运行代码；未重新训练。只核对原问题与这次失败涉及的实现文档条款、真实代码和缺失依据。
沿用本人可恢复的文档阅读记录，按需定位和读取当前代码；不要重读无关历史、重新提出已解决问题。
修改意见须明确：批准文档的具体位置、对应实现位置、违反的原要求、最小修复及验证方法。
原文支持具体修复才给 repair_code；缺少定义或证据应按原问题提交 needs_review/spec_conflict，
说明准确待补项，不能臆造测量方法、增加探针/拟合/调用、提高时间或内存额度、削弱方法或填写虚假通过。
代码原作者的 failed 回执是保留的失败信息，不是格式错误。缺失指标应追溯实际失败，不能要求伪造输出。
仅形成修订意见；宿主真实失败仍阻止接受，后续修改清单还须独立审核。'''


def _prepared(engine, run_id, state, specification, diagnostic):
    from .codex_delivery import inspect_delivery
    from .implementation_failures import ImplementationCodeBlocked

    def invalid():
        raise ImplementationCodeBlocked('执行失败复审缺少同版本原始证据，未派发。',
            kind='codex_execution_evidence_invalid', details={'workflow_id': state['id']})

    source = next((a for a in state['attempts'] if a['index'] == diagnostic.get('attempt')), None)
    if source is None or repair.unchanged_failed_execution(engine, run_id, state, source, specification) != diagnostic:
        invalid()
    static = inspect_delivery(Path(source['snapshot']['workspace']), specification['spec'])
    if not static['approved']:
        invalid()
    failed = engine.store.get('codex_checks', diagnostic['source_check'])
    if not failed:
        invalid()
    binding = {'workflow_id': state['id'], 'spec_hash': specification['spec_hash'],
        'document_sha256': specification['document']['sha256'],
        'source_check': diagnostic['source_check'], 'runtime_sha256': diagnostic['runtime_sha256']}
    marker = {'version': 1, 'fingerprint': repair.sha(binding), 'binding': binding,
        'diagnostic': copy.deepcopy(diagnostic), 'failed_check_sha256': repair.sha(failed),
        'source_snapshot_sha256': source['snapshot']['files_hash'],
        'review_only': True, 'execution_authorized': False, 'scientific_approval': False}
    acceptance = {**static, 'approved': False, 'issues': [diagnostic['message']],
        'runtime_sha256': diagnostic['runtime_sha256'], 'smoke': [copy.deepcopy(failed)],
        'execution_failure_review_only': True,
        'scope': '原宿主失败的同代码只读复审；未重新运行，未通过本地验收或科学验证。'}
    return {FIELD: marker, 'snapshot': copy.deepcopy(source['snapshot']), 'acceptance': acceptance}


def prepare(engine, run_id, state, specification, attempt, diagnostic):
    """A new review identity is allowed only for previously unreviewed evidence."""
    required = {'attempt', 'source_check', 'runtime_sha256', 'snapshot_sha256', 'repair_receipt_sha256'}
    if not required <= diagnostic.keys():
        return None
    prepared = _prepared(engine, run_id, state, specification, diagnostic)
    fingerprint = prepared[FIELD]['fingerprint']
    if any(a['index'] != attempt['index'] and a.get(FIELD, {}).get('fingerprint') == fingerprint
           for a in state['attempts']):
        return None  # Identical failure evidence cannot buy another review.
    if attempt.get(FIELD):
        validate(engine, run_id, state, specification, attempt)
        return prepared
    return prepared


def validate(engine, run_id, state, specification, attempt):
    """Reauthenticate the immutable source on resume; never cache an approval."""
    from .implementation_failures import ImplementationCodeBlocked
    actual = {key: attempt.get(key) for key in (FIELD, 'snapshot', 'acceptance')}
    expected = _prepared(engine, run_id, state, specification,
                         (attempt.get(FIELD) or {}).get('diagnostic', {}))
    if actual != expected:
        raise ImplementationCodeBlocked('执行失败复审的冻结绑定发生变化，未派发。',
            kind='codex_execution_evidence_invalid', details={'workflow_id': state['id'], 'attempt': attempt['index']})
    return expected[FIELD]
