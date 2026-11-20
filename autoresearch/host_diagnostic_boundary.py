"""Carry completed current-source checks into older host-diagnostic work.

Missing historical telemetry stays unknown. This continuation supplies actual
later evidence to the original reviewers; it never changes their decisions.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from . import code_repair_contract as repair

FIELD = 'host_diagnostic_boundary'
UPDATE = 'review_scope_update'


def current_checks(engine, run_id, state, attempt, original):
    from . import code_evidence_queue as queue, code_verification as checks
    from .code_check_invocation import inventory
    directory = Path(attempt['snapshot']['workspace'])
    manifest = json.loads(checks.checked_path(directory, 'implementation.json').read_bytes())
    expected = checks._inventory(directory, manifest)
    candidates = [ctx.get('host_verification', {}) for ctx in original['source_contexts'].values()]
    for key in attempt.get('evidence_task_ids', []):
        task = engine.store.get(queue.COLLECTION, key, {})
        binding = task.get('binding', {})
        if (task.get('run_id') == run_id and binding.get('workflow_id') == state['id']
                and binding.get('attempt') == attempt['index']
                and binding.get('spec_hash') == original['spec_hash']
                and binding.get('delivery_hash') == original['delivery_hash']):
            candidates.append(task.get('evidence', {}).get('host_verification', {}))
    result = {}
    for ref in candidates:
        if not ref.get('id') or ref.get('identity', {}).get('source_sha256') != expected:
            continue
        row = engine.store.get(checks.RECEIPTS, ref['id'], {})
        if (row.get('run_id') != run_id or row.get('sha256') != ref.get('sha256')
                or row.get('identity') != ref.get('identity')
                or row.get('status') != 'completed' or row.get('passed') is not True):
            continue
        checks._sealed_receipt(engine, row, row['identity'])
        frozen = row['identity'].get('frozen_delivery_sha256')
        if frozen is not None and frozen != inventory(directory, manifest):
            raise ValueError('Current host diagnostic check belongs to another frozen delivery')
        result[row['id']] = row
    return sorted(result.values(), key=lambda r: (r.get('finished_at', ''), r['id']))


def recover(engine, run_id, state, attempt, original, task):
    evidence = task.get('evidence', {})
    if (task.get('status') != 'blocked' or not task.get('review_record')
            or task.get('binding', {}).get('evidence_domain') != 'host_execution'
            or evidence.get(FIELD) or not evidence.get('host_receipt_facts')
            or not attempt.get('snapshot', {}).get('workspace')):
        return
    roles = sorted({v['role'] for v in task['review_record'].get('votes', [])
        if v.get('approve') is False and v.get('repair_findings')
        and all(f.get('disposition') == 'needs_review' for f in v['repair_findings'])})
    if not roles:
        return
    current = current_checks(engine, run_id, state, attempt, original)
    if not current:
        return
    expected = current[-1]['identity']['source_sha256']
    historical = [f for f in evidence['host_receipt_facts']
        if f.get('status') == 'failed' and f.get('host_exception_traceback_recorded') is False
        and isinstance(f.get('source_inventory'), dict) and f['source_inventory'] != expected]
    if not historical:
        return
    # A new completed group is material evidence. Do not pay for a repetition
    # merely to ask the same question against the same receipt again.
    known = {r.get('receipt_id') for r in evidence['host_receipt_facts']}
    known.add(evidence.get('host_verification', {}).get('id'))
    if all(r['id'] in known for r in current):
        return
    from . import code_evidence_queue as queue, code_repair_clarification as clarification
    from .code_followup import evidence as check_evidence
    clarification.verify_saved(engine, run_id, state, attempt, task['review_record'])
    boundary = {'version': 1, 'historical_receipts': [
        {k: f[k] for k in ('receipt_id', 'receipt_sha256', 'status',
            'host_exception_traceback_recorded', 'input_staging_recorded')} for f in historical],
        'current_check_receipts': [{'id': r['id'], 'sha256': r['sha256']} for r in current],
        'historical_cause': 'not_established', 'historical_success_claimed': False,
        'scientific_approval': False, 'training_authorized': False}
    body = {**copy.deepcopy(evidence), FIELD: boundary,
            'host_verification': check_evidence(current[-1], attempt)}
    update = {'version': 1, 'roles': roles, 'evidence_sha256': repair.sha(body)}
    history = copy.deepcopy(task.get('evidence_history', [])) + [{
        'handoff_id': task['handoff_id'], 'evidence': copy.deepcopy(evidence),
        'evidence_sha256': task['evidence_sha256'], UPDATE: copy.deepcopy(task.get(UPDATE))}]
    reviews = copy.deepcopy(task.get('review_history', [])) + [copy.deepcopy(task['review_record'])]
    task.pop('review_record')
    queue._save(engine, task, evidence=body, evidence_sha256=repair.sha(body),
        evidence_history=history, review_history=reviews, review_scope_update=update,
        status='evidence_ready', message='已转交同版本后续任务组的实际检查回执；仅原未决角色判断当前验收与历史未知的边界。')


GUIDANCE = ('\n本组 host_diagnostic_boundary 区分历史诊断和当前验收：旧版本未保存的调用栈、'
    '用例终态及因果关系仍为未知，不得补造、改成通过或要求反复检索不存在的日志。'
    'host_verification 和 current_check_receipts 是后续任务组已完成的同版本真实检查，沿用原报告和读取；'
    '当前没有授权重跑。请对照原批准条款独立判断当前交付是否仍有实际验收缺口。'
    '单独保留历史限制于 reasons 和原问题的处置说明，不把历史未知自动等同于当前源码缺陷。'
    '若该历史细节确是原验收的必要前置，保留意见并引用确切条款及尚缺的可执行处置；'
    '若原条款已被当前证据满足，可撤回当前阻断，同时明确历史失败未被解释或改写。'
    '不新增探针、补文档或泛化整轮评审；是否批准仍由本角色独立决定。')
