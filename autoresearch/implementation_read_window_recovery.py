"""One same-role recheck for legacy evidence-window starvation.

Original requests, receipts and UNKNOWN decisions remain immutable. A fresh
handoff must read and substantiate its own report through the normal validator.
This does not reset project usage, change criteria, or grant scientific credit.
"""
from __future__ import annotations

import copy

from . import document_read_window as window
from . import implementation_acceptance as policy
from .implementation_review_routing import digest

KEY = 'implementation_read_window_recovery'
GUIDANCE = ('\n验收阅读连续性：同一交接内已读取的精确版本正文及本角色笔记会持续保留，重叠范围只保留一次。'
    '按 required_sections 阅读当前验收项及其依赖，并补读决定结论所需的批准来源；不要把整份旧稿、差异目录或笔记目录当作必读正文。'
    '当前 tool_results 的正文和 document_read_fragments 是真实原文片段，可按其文档 SHA 和范围引用；'
    '已完整可见的范围不要重复读取。笔记和历史结论不能代替原文证据。'
    '逐项核对原 close_when，证据充分后输出完整报告；只有实际科学反例可以新增影响实现的阻断意见。')


def opt_in(context):
    context = copy.deepcopy(context)
    context['document_read_window_protocol'] = window.RETAINED_VERSION
    context['document_read_budget'] = {'page_chars': window.DOCUMENT_LIMIT,
        'prompt_policy': 'retain_exact_unique_reads_until_configured_model_input_capacity'}
    return context


def evidence_receipts(context, receipts):
    """Project frozen sources only; ancillary notes never prove starvation."""
    documents = {d['document_id'] for d in context['indexed_documents']}
    return [r for r in receipts if r.get('call', {}).get('name') in {'documents.read', 'documents.search'}
            and r.get('result', {}).get('document_id') in documents]


def diagnostic(engine, packet):
    context, result = packet['context'], packet['result']
    if (packet['role'] not in {'director', 'scout'} or not policy.enabled(context)
            or KEY in context or context.get('document_read_window_protocol') not in (1, 2)
            or policy.CONTEXT not in context or result.get('issues')):
        return None
    judgments = [*result.get('checks', []), *result.get('consistency', []),
        *(c for v in result.get('verifications', []) for c in v['conditions'])]
    if not any(c['status'] == 'UNKNOWN' for c in judgments) or any(c['status'] == 'FAIL' for c in judgments):
        return None
    progress = engine.store.get('handoff_progress', packet['handoff_id'], {})
    receipts = progress.get('tool_results', [])
    evidence = evidence_receipts(context, receipts)
    old = window.project(context, evidence).get('document_read_window', {})
    retained = window.project(opt_in(context), evidence).get('document_read_window', {})
    did = context[policy.CONTEXT]['document']['document_id']
    before = old.get('per_document_chars', {}).get(did, 0)
    after = retained.get('per_document_chars', {}).get(did, 0)
    if after <= before:
        return None
    return {'kind': 'legacy_review_read_window_starvation', 'document_id': did,
        'original_visible_chars': before, 'retained_read_chars': after,
        'source_ledger_sha256': window.sha(receipts),
        'finalization_reason': progress.get('finalization', {}).get('reason'),
        'scientific_approval': False}


def validate_source(engine, run_id, row, packet):
    from . import implementation_acceptance_flow as flow
    marker = packet['context'].get(KEY)
    if marker is None:
        return
    if (not isinstance(marker, dict) or marker.get('version') != 1
            or marker.get('attempt') != 1 or marker.get('attempt_limit') != 1
            or marker.get('scientific_approval') is not False):
        policy.fail('读取窗口恢复记录无效。')
    source_id = marker.get('source_handoff_id')
    frozen = engine.store.get('handoff_inputs', source_id, {})
    source = {'handoff_id': source_id, 'role': packet['role'],
        'result': marker.get('original_report'), 'context': frozen.get('context', {})}
    from .implementation_report_amendments import restore_provenance
    restore_provenance(source, marker)
    if KEY in source['context']:
        policy.fail('同一次读取窗口故障只能恢复一次。')
    flow.validate_report(engine, run_id, row, source)
    from .implementation_document_review_scope import replay_read_contract
    expected_context = opt_in(source['context'])
    expected_context[KEY] = copy.deepcopy(marker)
    source_handoff = engine.store.get('handoffs', source_id, {})
    purpose = source_handoff.get('purpose', '') + ':read-window-recovery:' + digest(source_id)[:16]
    before, after = replay_read_contract(expected_context, packet['context'], engine, run_id,
        packet=packet, purpose=purpose), packet['context']
    scope, prior = after[policy.CONTEXT], before[policy.CONTEXT]
    expected = diagnostic(engine, source)
    if (expected is None or marker.get('diagnostic') != expected
            or marker.get('source_result_sha256') != digest(source['result'])
            or after.get('document_read_window_protocol') != window.RETAINED_VERSION
            or any(after.get(k) != before.get(k) for k in ('acceptance_workflow_id', 'acceptance_budget',
                'indexed_documents', 'implementation_document_review_scope'))
            or any(scope.get(k) != prior.get(k) for k in prior if k != 'required_sections')
            or scope.get('required_sections') != (prior['required_sections'] if before.get('implementation_document_review_scope') else {cid: policy.required_sections(row, prior['contract'], [cid]) for cid in prior['assigned_criteria']})):
        policy.fail('读取窗口恢复改变了冻结身份、原结论或验收范围。')


