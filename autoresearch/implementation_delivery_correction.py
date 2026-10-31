"""One same-role correction for a report that omitted the current document.

The rejected decision and frozen request remain immutable. A new bounded handoff
must independently read its evidence; its result passes the ordinary validator.
"""
from __future__ import annotations

import copy

from . import implementation_acceptance as policy
from . import implementation_acceptance_flow as flow
from .implementation_document_delivery import prepare
from .implementation_review_routing import digest

KEY = 'implementation_delivery_correction'


async def validate_or_correct(engine, h, run_id, state, row, packet):
    from .engine import ResearchBlocked
    try:
        await engine._read_consensus_check(run_id, flow.validate_report, engine, run_id, row, packet)
        return packet
    except ResearchBlocked as exc:
        reading_diagnostic = getattr(exc, 'acceptance_read_coverage_error', None)
        if reading_diagnostic is not None:
            from .implementation_acceptance_read_recovery import recover
            return await recover(engine, h, run_id, state, row, packet, reading_diagnostic)
        source_diagnostic = getattr(exc, 'acceptance_source_reference_error', None)
        if source_diagnostic is not None:
            from .implementation_acceptance_citations import correct
            return await correct(engine, h, run_id, state, row, packet, source_diagnostic)
        diagnostic = getattr(exc, 'document_delivery_error', None)
        if diagnostic is None or KEY in packet['context']:
            raise
    # Exact original report is retained as rejected evidence, never silently fixed.
    source = engine.store.get('handoffs', packet['handoff_id'])
    context = copy.deepcopy(packet['context'])
    scope = context[policy.CONTEXT]
    scope.update(document_revision=row['index'], spec_hash=row['spec_hash'])
    context = prepare(engine, run_id, context)
    context[KEY] = {'version': 1, 'source_handoff_id': packet['handoff_id'],
        'source_result_sha256': digest(packet['result']), 'original_report': copy.deepcopy(packet['result']),
        'diagnostic': diagnostic, 'attempt': 1, 'attempt_limit': 1,
        'instruction': '原报告未通过当前文档引用校验。按交付清单重新阅读并核对所有 evidence，返回完整修正报告。'
            '不得猜测范围、复制他人阅读记录或将缺失证据改成 PASS；确无依据须如实说明。'}
    corrected = await flow.ask(engine, h, run_id, state,
        source['purpose'] + ':delivery-correction:' + digest(packet['handoff_id'])[:16], packet['role'],
        policy.GUIDANCE + '\n修正原报告的文档交付引用。全部判断仍须依据本交接真实阅读；这是一次引用修正，不是批准。',
        context, policy.REPORT_SCHEMA)
    return await validate_or_correct(engine, h, run_id, state, row, corrected)
