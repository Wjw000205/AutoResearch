"""One bounded same-role recheck after a premature PASS with missing reads.

Never changes an original report, request, receipt or read credit. A new review
uses the exact same criterion/issue contract and earns its own delivery credit.
"""
from __future__ import annotations

import copy

from . import implementation_acceptance as policy
from .implementation_review_routing import digest

KEY = 'implementation_acceptance_read_recovery'


def context_for(engine, run_id, row, packet, diagnostic):
    from . import implementation_acceptance_flow as flow
    from .implementation_read_window_recovery import opt_in
    from .implementation_acceptance_reading import PLAN
    from .engine import ResearchBlocked
    context = flow.check_packet(engine, run_id, packet)
    if KEY in context or 'citation_correction' in packet:
        policy.fail('同一审查报告的缺失读取只能补核一次；原报告保留。')
    if (not isinstance(diagnostic, dict) or diagnostic.get('version') != 1
            or diagnostic.get('kind') != 'incomplete_acceptance_reading' or not diagnostic.get('criteria')):
        policy.fail('缺失读取补核必须绑定精确的非空门禁诊断。')
    # Authenticate every existing reference and all non-coverage gates first.
    try:
        flow.validate_report(engine, run_id, row, packet)
    except ResearchBlocked as exc:
        if getattr(exc, 'acceptance_read_coverage_error', None) is None or exc.acceptance_read_coverage_error != diagnostic:
            raise
    else:
        policy.fail('原报告没有匹配的实际读取缺口。')
    ctx = opt_in(context)
    scope = ctx[policy.CONTEXT]
    scope['required_sections'] = (scope['required_sections'] if context.get('implementation_document_review_scope') else {cid: policy.required_sections(row, scope['contract'], [cid]) for cid in scope['assigned_criteria']})
    ctx[PLAN] = 1
    ctx[KEY] = {'version': 1, 'source_handoff_id': packet['handoff_id'],
        'source_result_sha256': digest(packet['result']), 'original_report': copy.deepcopy(packet['result']),
        'source_read_ledger_sha256': digest(engine.store.get('handoff_progress', packet['handoff_id'], {}).get('tool_results', [])),
        'diagnostic': copy.deepcopy(diagnostic), 'attempt': 1, 'attempt_limit': 1, 'scientific_approval': False,
        'instruction': '原报告提前给出 PASS，但没有完成实际读取。保持同一角色、文档、合同、问题及关闭条件；'
            '按修正后的 required_sections 和 acceptance_reading_progress 读取本项及依赖，并核对决定结论的批准来源。'
            '旧报告只用于定位原结论和问题，不是本交接的阅读信用。必须真实读取后返回完整独立报告。'
            '优先验证原关闭条件，发现反例如实给 FAIL/UNKNOWN；不能把旧 PASS 自动当作通过，不增加无关意见。'}
    from .implementation_report_amendments import provenance
    if provenance(packet):
        ctx[KEY]['source_provenance'] = provenance(packet)
    return ctx


def validate_source(engine, run_id, row, packet):
    marker = packet['context'].get(KEY)
    if marker is None:
        return
    if (not isinstance(marker, dict) or marker.get('version') != 1
            or marker.get('attempt') != 1 or marker.get('attempt_limit') != 1
            or marker.get('scientific_approval') is not False):
        policy.fail('缺失读取补核的冻结出处无效。')
    # A later current-document citation correction keeps this ancestry, but
    # earns all its own reads. Authenticate the intervening immutable report.
    from . import implementation_delivery_correction as delivery
    repair = packet['context'].get(delivery.REPAIR)
    if repair:
        # A later semantic correction earns its own reads while keeping this
        # recovery's authenticated ancestry. check_packet validates the new link.
        parent = {'handoff_id': repair['source_handoff_id'], 'role': packet['role'],
            'result': repair['original_result'], 'context': engine.store.get(
                'handoff_inputs', repair['source_handoff_id'], {}).get('context', {})}
        from .implementation_report_amendments import restore_provenance
        restore_provenance(parent, {'source_provenance': {
            k: repair[k] for k in ('citation_correction', 'report_amendment') if k in repair}})
        if parent['context'].get(KEY) == marker:
            delivery.validate_repair(engine, run_id, packet)
            validate_source(engine, run_id, row, parent)
            return
    correction = packet['context'].get(delivery.KEY)
    if correction is not None:
        from . import implementation_acceptance_flow as flow
        from .implementation_document_delivery import prepare
        from .engine import ResearchBlocked
        parent_id = correction.get('source_handoff_id')
        if parent_id == packet['handoff_id']:
            policy.fail('补核引用修正的前驱不能是自身。')
        parent = {'handoff_id': parent_id, 'role': packet['role'], 'result': correction.get('original_report'),
            'context': engine.store.get('handoff_inputs', parent_id, {}).get('context', {})}
        from .implementation_report_amendments import restore_provenance
        restore_provenance(parent, correction)
        # Only treat a correction added AFTER this recovery as an intervening
        # handoff. A marker inherited from an earlier correction stays frozen.
        if parent['context'].get(KEY) == marker and delivery.KEY not in parent['context']:
            flow.check_packet(engine, run_id, parent)
            try:
                flow.validate_report(engine, run_id, row, parent)
            except ResearchBlocked as exc:
                if (getattr(exc, 'document_delivery_error', None) is None
                        or exc.document_delivery_error != correction.get('diagnostic')):
                    raise
            else:
                policy.fail('补核引用修正没有原始失败诊断。')
            expected = copy.deepcopy(parent['context'])
            expected[policy.CONTEXT].update(document_revision=row['index'], spec_hash=row['spec_hash'])
            expected = prepare(engine, run_id, expected)
            expected[delivery.KEY] = copy.deepcopy(correction)
            from .implementation_document_review_scope import replay_read_contract
            before = engine.store.get('handoffs', parent_id, {})
            purpose = before.get('purpose', '') + ':delivery-correction:' + digest(parent_id)[:16]
            expected = replay_read_contract(expected, packet['context'], engine, run_id, packet=packet, purpose=purpose)
            if (packet['context'] != expected
                    or correction.get('source_result_sha256') != digest(parent['result'])
                    or correction.get('version') != 1 or correction.get('attempt') != 1
                    or correction.get('attempt_limit') != 1):
                policy.fail('补核引用修正改变了冻结来源或范围。')
            return
    source_id = marker.get('source_handoff_id')
    source = {'handoff_id': source_id, 'role': packet['role'], 'result': marker.get('original_report'),
              'context': engine.store.get('handoff_inputs', source_id, {}).get('context', {})}
    from .implementation_report_amendments import restore_provenance
    restore_provenance(source, marker)
    expected = context_for(engine, run_id, row, source, marker.get('diagnostic'))
    from .implementation_document_review_scope import replay_read_contract
    before = engine.store.get('handoffs', source_id, {})
    purpose = before.get('purpose', '') + ':read-coverage-recovery:' + digest(source_id)[:16]
    expected = replay_read_contract(expected, packet['context'], engine, run_id, packet=packet, purpose=purpose)
    if expected != packet['context']:
        policy.fail('缺失读取补核改变了原任务、冻结范围或读取出处。')


