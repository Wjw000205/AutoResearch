"""Author save continuity: inherited retrieval still needs current delivery.

The generic reading primitive intentionally ignores predecessor receipts. Only
the authenticated save successor may use its immutable predecessor retrievals,
and only fragments actually submitted to this successor's engineer count.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from .implementation_review_routing import digest


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现作者继承读取：' + message)


def _assignment(engine, run_id, hid, context):
    from . import implementation_authoring as author
    from .engine import Harness
    assignment, frozen, state, binding = author._assignment(engine, run_id, 'engineer', hid)
    inputs = engine.store.get('handoff_inputs', hid, {})
    payload = {key: inputs.get(key) for key in ('instruction', 'context', 'schema')}
    if (assignment.get('id') != hid or inputs.get('id') != hid or inputs.get('run_id') != run_id
            or frozen != context or assignment.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, 'engineer', assignment.get('purpose'), **payload) != hid
            or inputs.get('schema') != author.submission_schema(context)
            or state.get('current_revision') != binding.get('revision')):
        _fail('当前作者、冻结输入或工作流版本不一致。')
    return assignment, inputs, state, binding


def _validate_lineage(engine, run_id, hid, context):
    """Authenticate the exact seeded prefix without changing any checkpoint."""
    from . import implementation_authoring_completion as completion
    assignment, _, state, binding = _assignment(engine, run_id, hid, context)
    contract = context.get('implementation_document_completion', {})
    if not isinstance(contract, dict):
        _fail('保存交接的前驱合同已损坏。')
    source_id = contract.get('source_handoff_id')
    if (not isinstance(source_id, str) or not source_id or source_id == hid
            or contract.get('phase') != 'save' or contract.get('scientific_approval') is not False
            or contract.get('predecessor_audit_id') != source_id + ':save-predecessor:v1'
            or type(contract.get('saved_read_receipts')) is not int):
        _fail('保存交接缺少精确的前驱身份。')
    source_inputs = engine.store.get('handoff_inputs', source_id, {})
    source_context = source_inputs.get('context', {})
    source, _, source_state, source_binding = _assignment(engine, run_id, source_id, source_context)
    if (completion.enabled(source_context) or source_state.get('id') != state.get('id') or source_binding != binding
            or assignment.get('purpose') != source.get('purpose') + ':save:1'):
        _fail('前驱不是本角色同版本的原编写交接。')
    audit_id = contract['predecessor_audit_id']
    audit = engine.store.get('stage_contexts', audit_id, {})
    payload = audit.get('payload', {})
    if not isinstance(payload, dict):
        _fail('前驱归档正文已损坏。')
    source_progress = engine.store.get('handoff_progress', source_id, {})
    progress = engine.store.get('handoff_progress', hid, {})
    if (audit.get('id') != audit_id or audit.get('run_id') != run_id
            or audit.get('successor_id') != hid or audit.get('scientific_approval') is not False
            or audit.get('sha256') != contract.get('predecessor_sha256')
            or digest(payload) != audit.get('sha256')
            or payload.get('input') != source_inputs or payload.get('progress') != source_progress
            or source_progress.get('id') != source_id or source_progress.get('run_id') != run_id
            or progress.get('id') != hid or progress.get('run_id') != run_id
            or progress.get('predecessor_audit_id') != audit_id):
        _fail('前驱归档、原始输入或读取账本已改变。')
    allowed = completion._inherited_scope(engine, SimpleNamespace(engine=engine), run_id,
        source_id, source_context, source_progress, source_state, source_binding)
    expected = []
    for receipt in source_progress.get('tool_results', []):
        if not isinstance(receipt, dict):
            _fail('前驱原文回执已损坏。')
        call, page = receipt.get('call', {}), receipt.get('result', {})
        if not isinstance(call, dict) or call.get('name') not in {'documents.read', 'documents.search'}:
            continue
        if not isinstance(page, dict):
            _fail('前驱原文正文回执已损坏。')
        if page.get('error'):
            continue
        if receipt.get('predecessor_handoff_id') is not None:
            _fail('原编写交接不能把其他前驱的回执转为本角色首次读取。')
        expected.append({**copy.deepcopy(receipt), 'predecessor_handoff_id': source_id})
    rows = progress.get('tool_results', [])
    if (not isinstance(rows, list) or contract['saved_read_receipts'] != len(expected)
            or rows[:len(expected)] != expected
            or any(not isinstance(row, dict) or row.get('predecessor_handoff_id') is not None
                   for row in rows[len(expected):])):
        _fail('继承读取回执不是已封存前驱的完整原始清单。')
    return expected, allowed


def _lineage(engine, run_id, hid, context):
    # This proof can be expensive because the predecessor's reviewed baseline
    # is authenticated as well. A single immutable read snapshot may reuse it;
    # live mutable stores never retain or reuse the proof.
    if getattr(engine.store, 'is_read_snapshot', False):
        key = ('implementation_author_inherited_lineage', run_id, hid, digest(context))
        return engine.store.memoized_read(key,
            lambda: _validate_lineage(engine, run_id, hid, context))
    return _validate_lineage(engine, run_id, hid, context)


def coverage(engine, run_id, document, required, handoff_id, context):
    """Match ordinary coverage plus authenticated, currently delivered inheritance."""
    from . import document_index, implementation_reading as reading
    from .implementation_authoring_completion import enabled
    from .evidence_delivery import delivery_spans
    ordinary = reading.coverage(engine, run_id, document, required, [handoff_id])
    if ordinary['complete'] or not enabled(context):
        return ordinary
    inherited, allowed = _lineage(engine, run_id, handoff_id, context)
    did, sha = document['document_id'], document['sha256']
    spans = {}
    for row in ordinary['covered_ranges']:
        spans.setdefault(row['section_id'], []).append((row['start'], row['end']))
    manifest, text = document_index.load(engine, run_id, did, sha)
    sections = {section['id']: section for section in manifest['sections']}
    # No legacy/untracked exception: inheritance always needs a sealed current
    # request acknowledgment, even when ordinary local reads use legacy credit.
    delivered = delivery_spans(engine.store, run_id, 'engineer', did, sha,
        identity_key='document_id', handoff_id=handoff_id, text=text)
    for receipt in inherited:
        call, page = receipt['call'], receipt['result']
        args = call.get('arguments', {})
        if call['name'] != 'documents.read' or page.get('document_id') != did:
            continue
        if not isinstance(args, dict):
            _fail('继承读取参数已损坏。')
        directory = allowed.get(did, {})
        section = sections.get(args.get('section_id')) if isinstance(args, dict) else None
        start, end = page.get('char_start'), page.get('char_end')
        offset, limit = args.get('offset', 0), args.get('limit', 4000)
        if (directory.get('sha256') != sha or args.get('document_id') != did
                or args.get('expected_sha256') != sha or page.get('sha256') != sha
                or section is None or page.get('section_id') != section['id']
                or type(start) is not int or type(end) is not int
                or type(offset) is not int or type(limit) is not int or not 1 <= limit <= 6000
                or not section['start'] <= start < end <= section['end']
                or start != section['start'] + offset or end > start + limit
                or page.get('text') != text[start:end]):
            _fail('继承回执的授权版本、范围或正文不一致。')
        for row in delivered:
            if row.get('section_id') == section['id']:
                a, b = max(start, row['char_start']), min(end, row['char_end'])
                if a < b:
                    spans.setdefault(section['id'], []).append((a, b))
    covered, missing = [], []
    for receipt in required:
        page = receipt['result']
        start, end, sid = page['char_start'], page['char_end'], page['section_id']
        cursor = start
        for a, b in sorted(spans.get(sid, [])):
            a, b = max(start, a), min(end, b)
            if b <= cursor or b <= a:
                continue
            if a > cursor:
                missing.append({'section_id': sid, 'start': cursor, 'end': a})
                cursor = a
            covered.append({'section_id': sid, 'start': cursor, 'end': b})
            cursor = b
        if cursor < end:
            missing.append({'section_id': sid, 'start': cursor, 'end': end})
    return {'covered_ranges': covered, 'missing_ranges': missing,
        'covered_chars': sum(row['end'] - row['start'] for row in covered), 'complete': not missing}
