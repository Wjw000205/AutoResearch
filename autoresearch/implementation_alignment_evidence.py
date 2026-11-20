"""Verify version-bound source reading behind implementation alignment claims.

This check only consumes immutable documents and existing handoff ledgers. A
directory, another role's reading, or an older specification grants no credit.
"""
from __future__ import annotations

from contextvars import ContextVar

from . import document_index
from .architecture_validation import operation, once

PROTOCOL = 'implementation_alignment_protocol'
from .implementation_reading import READ_PENDING
_candidate = ContextVar('implementation_alignment_candidate', default=None)


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked(message)


def _workflow_design(engine, run_id, hid, context):
    """Resolve the host workflow; model-supplied design bodies grant no scope."""
    from .engine import digest
    bindings = [context.get('implementation_document_authoring') or {},
        context.get('whole_code_review') or {},
        context.get('definition_code_followup') or {},
        context.get('code_repair_clarification') or {},
        (context.get('implementation_indexed_review_contract') or {}).get('binding') or {},
        context.get('implementation_issue_response_contract') or {}]
    identities = {binding.get('workflow_id') for binding in bindings if binding.get('workflow_id')}
    if len(identities) > 1:
        _fail('对齐依据的冻结工作流身份不一致。')
    if not identities:
        purpose = engine.store.get('handoffs', hid, {}).get('purpose', '')
        parts = purpose.split(':') if isinstance(purpose, str) else []
        if parts and parts[0] == 'code_review' and (len(parts) == 3 or len(parts) == 5 and parts[3] == 'redispatch'):
            identities = {key for key in engine.store.record_keys('codex_workflows', run_id)
                          if digest(key) == parts[1]}
            if len(identities) > 1:
                _fail('代码对齐依据缺少同一宿主工作流。')
    if not identities:
        return None  # Historic primary-only evidence has no host inheritance.
    key = next(iter(identities))
    state = engine.store.implementation_workflow_design(key)
    if not state:
        return None  # Frozen primary bindings need no new workflow lookup gate.
    if state.get('id') != key or state.get('run_id') != run_id:
        _fail('对齐依据的宿主工作流不属于本任务。')
    return (state.get('common') or {}).get('design')


def _sources(context, design=None):
    from .implementation_alignment import _sources as source_documents
    return source_documents(context, design)


def _delivery(engine, run_id, role, hid, result):
    from .engine import Harness, digest
    if not isinstance(hid, str) or not hid:
        _fail('对齐审查缺少原始交接身份。')
    assignment = engine.store.get('handoffs', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    decision = engine.store.get('decisions', hid, {})
    if decision.get('code_review_inheritance'):
        from .review_inheritance import verify_decision
        verify_decision(engine, run_id, role, hid, frozen.get('context', {}), decision)
    payload = {key: frozen.get(key) for key in ('instruction', 'context', 'schema')}
    candidate = _candidate.get()
    source = frozen.get('context') or {}
    delivered = result
    checking_candidate = bool(candidate and candidate[:6] == (
        id(engine), run_id, role, hid, digest(delivered), digest(frozen.get('context'))))
    if (assignment.get('id') != hid or assignment.get('run_id') != run_id
            or assignment.get('to_role') != role or frozen.get('id') != hid
            or frozen.get('run_id') != run_id or assignment.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, role, assignment.get('purpose'), **payload) != hid):
        _fail('对齐审查的角色、冻结输入或原始交付记录不一致。')
    if not checking_candidate and (
            assignment.get('status') != 'completed' or assignment.get('output_hash') != digest(delivered)
            or decision.get('id') != hid or decision.get('run_id') != run_id
            or decision.get('role') != role or decision.get('purpose') != assignment.get('purpose')
            or decision.get('result') != delivered):
        _fail('对齐审查的角色、冻结输入或原始交付记录不一致。')
    # A genuinely empty checkpoint has no reads to authenticate yet. A
    # nonempty ledger must still carry its exact owner before any credit.
    from .implementation_reading import ledger_identity
    owner, owner_run = ledger_identity(engine.store, hid)
    if owner is not None and (owner != hid or owner_run != run_id):
        _fail('对齐审查的真实读取记录不属于当前交接。')
    for key in ('proposal_hash', 'spec_hash', 'document_sha256', 'code_sha256', 'delivery_hash'):
        if key in source and (key in result or key != 'document_sha256') and result.get(key) != source[key]:
            _fail('对齐审查原始结论没有绑定冻结输入的同一版本。')
    if isinstance(result.get('alignment_binding'), dict):
        contract = source.get('implementation_alignment_contract') or {}
        if any(result['alignment_binding'].get(key) != contract.get(key) for key in
               ('design_hash', 'experiment_plan_hash', 'architecture_document_sha256')):
            _fail('对齐审查原始结论的架构或计划绑定不一致。')
    return source


@once
def _handoffs(engine, run_id, role, hid, result, context):
    from .engine import digest
    frozen = _delivery(engine, run_id, role, hid, result)
    if digest(frozen) != digest(context):
        _fail('对齐审查上下文与原始冻结输入不一致。')
    from . import independent_full_review as full
    if full.KEY in context:
        return _full_handoffs(engine, run_id, role, hid, context)
    from .review_inheritance import owners
    return [hid, *owners(engine, run_id, role, hid, context)]


def _full_handoffs(engine, run_id, role, hid, context):
    from . import independent_full_review as full
    decision = engine.store.get('decisions', hid, {})
    if decision.get('result') is not None:
        return full.reading_handoffs(engine.store, run_id,
            full.stamp_vote(role, decision['result'], hid, context[full.KEY]))
    # The ordinary candidate validator owns the current unsealed request.
    # Only a completed, authenticated same-source parent can extend its reads.
    parent = context.get(full.READING, {}).get('parent_handoff_id')
    if not parent:
        return [hid]
    before = engine.store.get('handoff_inputs', parent, {}).get('context', {})
    raw = engine.store.get('decisions', parent, {}).get('result')
    if (before.get(full.KEY) != context[full.KEY] or full.material_text(before) != full.material_text(context)
            or full.sha(raw) != context[full.READING].get('parent_result_sha256')):
        _fail('Full-review source continuation changed its original parent material')
    return [hid, *full.reading_handoffs(engine.store, run_id, full.stamp_vote(role, raw, parent, context[full.KEY]))]


def _required_ranges(engine, run_id, refs, sources):
    if not isinstance(refs, list) or not refs:
        _fail(READ_PENDING + '；需要可读取的原文位置。')
    required = {}
    documents = {}
    for ref in refs:
        if not isinstance(ref, dict):
            _fail('对齐依据引用必须包含精确文档、版本及字符范围。')
        did, sha = ref.get('document_id'), ref.get('document_sha256')
        if did not in sources or sources[did] != sha:
            _fail('对齐依据引用了未绑定的文档或旧版本。')
        try:
            # One authenticated load per exact identity in this synchronous
            # validation. Never retain file-integrity credit across calls.
            if (did, sha) not in documents:
                manifest, _ = document_index.load(engine, run_id, did, sha)
                documents[did, sha] = {s['id']: s for s in manifest['sections']}
        except (ValueError, OSError) as exc:
            _fail('对齐依据原文的版本或完整性校验失败：' + str(exc))
        section = documents[did, sha].get(ref.get('section_id'))
        offset, limit = ref.get('offset'), ref.get('limit')
        if (section is None or type(offset) is not int or type(limit) is not int
                or offset < 0 or not 1 <= limit <= 6000
                or offset + limit > section['end'] - section['start']):
            _fail('对齐依据引用不是当前原文中的有效字符范围。')
        start = section['start'] + offset
        required.setdefault(did, []).append({'result': {
            'section_id': section['id'], 'char_start': start, 'char_end': start + limit}})
    return required


@operation
def validate_review_evidence(engine, run_id, role, handoff_id, result, context):
    """Return a small receipt report, or block unsupported ``aligned`` claims.

    Semantic completeness and mismatch handling belong to the alignment schema
    policy. Missing reading is a same-version continuation, never a rewrite.
    """
    if context.get(PROTOCOL) != 1:
        return None
    source_documents = _sources(context, _workflow_design(engine, run_id, handoff_id, context))
    sources = {did: doc['sha256'] for did, doc in source_documents.items()}
    findings = result.get('alignment_findings')
    if not isinstance(findings, list):
        _fail('对齐审查缺少结构化的逐项核对结果。')
    handoffs = _handoffs(engine, run_id, role, handoff_id, result, context)
    required = {}
    count = 0
    legacy_code_changes = ('delivery_artifacts' in context and 'repair_findings' not in result
                           and bool(result.get('fixes')))
    for finding in findings:
        if not isinstance(finding, dict):
            _fail('对齐审查的逐项核对结果格式不完整。')
        refs = finding.get('source_refs')
        if not isinstance(refs, list) or finding.get('status') == 'aligned' and not refs:
            _fail(READ_PENDING + '；已对齐项需要可读取的原文位置。')
        if refs:
            from .implementation_alignment import _validate_refs
            _validate_refs(refs, source_documents)
        ranges = _required_ranges(engine, run_id, refs, sources) if refs else {}
        if finding.get('status') == 'aligned' or legacy_code_changes and finding.get('status') == 'pending':
            count += len(refs)
            for did, pages in ranges.items():
                required.setdefault(did, []).extend(pages)
    from .implementation_reading import coverage
    for did, ranges in required.items():
        report = coverage(engine, run_id,
            {'document_id': did, 'sha256': sources[did]}, ranges, handoffs)
        if not report['complete']:
            _fail(READ_PENDING + '；请按 source_refs 补齐本角色实际读取，保留当前规范与原意见。')
    return {'verified_source_refs': count, 'reading_handoff_ids': handoffs,
            'same_version': True, 'scientific_approval': False}


def _code_text(engine, run_id, artifact_id, delivery, *, allow_binary=False):
    import hashlib
    from pathlib import Path
    from .deletion import _plain_path
    artifact = engine.store.get('artifacts', artifact_id, {})
    if artifact.get('id') != artifact_id or artifact.get('run_id') != run_id:
        _fail('代码对齐依据不属于当前任务的实际交付。')
    try:
        path = Path(artifact['path']).absolute()
        _plain_path(path, engine.data_dir)
        if not path.resolve().is_relative_to(engine.run_dir(run_id)):
            _fail('代码对齐依据路径不属于当前任务。')
        from .delivery_files import io_path
        raw = io_path(path).read_bytes()
        if (artifact.get('sha256') != delivery.get('sha256')
                or hashlib.sha256(raw).hexdigest() != delivery.get('sha256')
                or type(delivery.get('bytes')) is not int or len(raw) != delivery['bytes']):
            _fail('代码对齐依据的实际字节、版本或大小与冻结交付不一致。')
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            if allow_binary:
                return None
            raise
    except (KeyError, ValueError, OSError) as exc:
        _fail('代码对齐依据无法核验实际交付原文：' + str(exc))


def _own_code_spans(engine, handoff_id, artifact_id, sha, text):
    from .architecture_validation import source_read
    import json

    def read_receipts():
        # The checkpoint also contains full prepared prompts and reply history.
        # Select only the durable read ledger, once per validation operation.
        with engine.store._read_db() as db:
            row = db.execute("SELECT json_extract(data,'$.tool_results') FROM records "
                "WHERE collection='handoff_progress' AND key=?", (handoff_id,)).fetchone()
        receipts = json.loads(row[0]) if row and row[0] is not None else []
        if not isinstance(receipts, list) or any(not isinstance(item, dict) for item in receipts):
            _fail('代码对齐的实际读取回执格式损坏。')
        return [item for item in receipts if isinstance(item.get('call'), dict)
                and item['call'].get('name') in {'artifacts.read', 'code.read', 'materials.request_read'}]

    spans = []
    receipts = source_read(engine.store, ('code_read_receipts', handoff_id), read_receipts, borrowed=True)
    for receipt in receipts:
        if not isinstance(receipt, dict):
            _fail('代码对齐的实际读取回执格式损坏。')
        if receipt.get('call', {}).get('name') == 'materials.request_read':
            if receipt.get('result', {}).get('error'):
                continue
            from .supplemental_reads import normal_read
            owner = engine.store.get('handoffs', handoff_id, {})
            receipt = normal_read(engine, owner['run_id'], handoff_id, receipt)
        call, page = receipt.get('call'), receipt.get('result')
        if not isinstance(call, dict) or call.get('name') not in {'artifacts.read', 'code.read'}:
            continue
        args = call.get('arguments')
        if not isinstance(args, dict):
            _fail('代码对齐的实际读取参数格式损坏。')
        if args.get('id') != artifact_id:
            continue
        if isinstance(page, dict) and page.get('error'):
            continue
        from .code_review_reread import withheld
        if withheld(page):
            continue  # Typed nonexecution is never an actual code-read receipt.
        default_limit = 4000 if call['name'] == 'code.read' else 16000
        offset, limit = args.get('offset', 0), args.get('limit', default_limit)
        if (not isinstance(page, dict) or type(offset) is not int or offset < 0
                or offset > len(text) or type(limit) is not int or not 1 <= limit <= 16000):
            _fail('代码对齐的实际读取范围不合法。')
        end = min(offset + limit, len(text))
        if (args.get('expected_sha256', sha) != sha or page.get('artifact_id') != artifact_id
                or page.get('sha256') != sha or type(page.get('offset')) is not int
                or page['offset'] != offset or type(page.get('total_chars')) is not int
                or page['total_chars'] != len(text) or page.get('text') != text[offset:end]
                or page.get('next_offset') != (end if end < len(text) else None)
                or page.get('truncated') is not (end < len(text))):
            _fail('代码对齐的实际读取回执与冻结交付的版本、范围或原文字节不一致。')
        spans.append((offset, end))
    return sorted(spans)


def _code_spans(engine, handoff_id, artifact_id, sha, text):
    spans = _own_code_spans(engine, handoff_id, artifact_id, sha, text)
    from . import independent_full_review as full
    from .architecture_validation import source_read
    def full_context():
        import json
        with engine.store._read_db() as db:
            row = db.execute("SELECT CASE WHEN json_type(data,'$.context.independent_full_review') IS NOT NULL "
                "THEN json_extract(data,'$.context') END FROM records "
                "WHERE collection='handoff_inputs' AND key=?", (handoff_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else {}
    context = source_read(engine.store, ('full_code_read_context', handoff_id), full_context, borrowed=True)
    if full.KEY in context:
        owner = engine.store.get('handoffs', handoff_id, {})
        for parent in _full_handoffs(engine, owner['run_id'], owner['to_role'], handoff_id, context)[1:]:
            spans.extend(_own_code_spans(engine, parent, artifact_id, sha, text))
        return sorted(spans)
    from .review_inheritance import raw_plan
    if raw_plan(engine, handoff_id) is None:
        return spans
    hand = engine.store.get('handoffs', handoff_id, {})
    context = engine.store.get('handoff_inputs', handoff_id, {}).get('context', {})
    from .review_inheritance import owners, consumed_receipts
    from .evidence_delivery import tracking_required
    from .working_context import sha as receipt_sha
    for owner in owners(engine, hand.get('run_id'), hand.get('to_role'), handoff_id, context):
        old = _own_code_spans(engine, owner, artifact_id, sha, text)
        if not tracking_required(engine.store, hand['run_id'], owner):
            spans.extend(old)  # Preserve the exact legacy reading contract.
            continue
        def read_progress():
            import json
            with engine.store._read_db() as db:
                row=db.execute("SELECT json_extract(data,'$.tool_results') FROM records "
                    "WHERE collection='handoff_progress' AND key=?",(owner,)).fetchone()
            return {'tool_results':json.loads(row[0]) if row and row[0] is not None else []}
        from .architecture_validation import source_read
        progress = source_read(engine.store,('inherited_code_tool_results',owner),read_progress,borrowed=True)
        consumed = consumed_receipts(engine, hand['run_id'], hand['to_role'], owner)
        # A sealed original opinion retains its original reading contract.
        # Its exact cited ranges were already admitted with the actual ledger;
        # later prompt-window eviction cannot revoke that completed evidence.
        verdict = engine.store.get('decisions', owner, {}).get('result', {})
        for citation in verdict.get('code_evidence', []):
            if citation.get('artifact_id') == artifact_id and citation.get('sha256') == sha:
                begin, end = citation['offset'], citation['offset'] + citation['limit']
                cursor = begin
                for a, b in old:
                    if a <= cursor:
                        cursor = max(cursor, b)
                if cursor >= end:
                    spans.append((begin, end))
        for row in progress.get('tool_results', []):
            call, page = row.get('call', {}), row.get('result', {})
            if (call.get('name') in {'code.read', 'artifacts.read'} and call.get('arguments', {}).get('id') == artifact_id
                    and receipt_sha(row) in consumed and isinstance(page.get('text'), str) and not page.get('error')):
                start = page['offset']
                spans.append((start, start + len(page['text'])))
    return sorted(spans)


@operation
def validate_code_evidence(engine, run_id, role, handoff_id, result, context):
    """Check actual same-role code reads; a verification description is no run proof."""
    if context.get(PROTOCOL) != 1 or 'delivery_artifacts' not in context:
        return None
    from pathlib import PurePosixPath
    from .engine import ResearchBlocked, digest
    from .implementation_alignment import acceptance_ids, required_review_ids
    frozen = _delivery(engine, run_id, role, handoff_id, result)
    if digest(frozen) != digest(context):
        _fail('代码对齐上下文与原始冻结输入不一致。')
    assignment = engine.store.get('handoffs', handoff_id, {})
    if str(assignment.get('purpose', '')).split(':', 1)[0] != 'code_review':
        _fail('实际代码依据只接受当前代码评审交接中的读取。')
    deliveries = context.get('delivery_artifacts')
    if not isinstance(deliveries, list) or any(not isinstance(row, dict) for row in deliveries):
        _fail('代码对齐缺少当前冻结交付目录。')
    by_id = {row.get('artifact_id'): row for row in deliveries}
    if len(by_id) != len(deliveries) or any(not isinstance(key, str) or not key for key in by_id):
        _fail('代码对齐冻结交付存在重复或缺失的文件身份。')
    items = result.get('code_evidence')
    if not isinstance(items, list):
        _fail('代码对齐审查缺少实际代码依据。')
    accepted = acceptance_ids(context)
    required = required_review_ids(context,role)
    from . import code_review_partition as partitions
    partitioned = partitions.FIELD in context
    if result.get('approve') is True and (not accepted or not items) and (not partitioned or required):
        _fail(READ_PENDING + '；代码批准需要逐项核对验收 ID 对应的实际实现。')
    import json
    manifest_files = [row for row in deliveries if row.get('path') == 'implementation.json']
    manifest_rows = context.get('implementation_alignment_manifest_requirements')
    if partitioned:
        try:
            batch = partitions.authenticate(engine, run_id, role, handoff_id, context)
            partitions.validate_ballot(context, role, result)
        except ValueError as exc:
            _fail(str(exc))
        manifest_rows = batch['requirements']
    if len(manifest_files) != 1 or not isinstance(manifest_rows, list):
        _fail('代码对齐缺少本次实际交付清单中的验收映射。')
    manifest_file = manifest_files[0]
    try:
        manifest = json.loads(_code_text(engine, run_id, manifest_file['artifact_id'], manifest_file))
    except (ValueError, KeyError) as exc:
        _fail('代码对齐无法核对本次实际交付清单：' + str(exc))
    if not isinstance(manifest, dict) or manifest.get('requirements') != manifest_rows:
        _fail('代码对齐验收映射与实际冻结交付清单不一致。')
    locations = {}
    for row in manifest_rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in locations
                or not isinstance(row.get('locations'), list) or not row['locations']
                or any(not isinstance(path, str) or not path for path in row['locations'])):
            _fail('代码对齐的实际交付验收映射不完整或重复。')
        locations[row['id']] = {path.split(':', 1)[0] for path in row['locations']}
    full_ids = context.get('definition_code_followup',{}).get('full_acceptance_ids',accepted)
    if accepted and (not set(accepted)<=set(full_ids) or set(locations)!=set(full_ids)):
        _fail('代码对齐的实际交付清单未对应同一规范的全部验收 ID。')
    seen, loaded, spans, implementation_ranges = set(), {}, {}, {}
    implementation = context.get('implementation_document') or {}
    if (not implementation.get('document_id') or not implementation.get('sha256')
            or not any(doc.get('document_id') == implementation['document_id']
                       and doc.get('sha256') == implementation['sha256']
                       for doc in context.get('indexed_documents', []))):
        _fail('代码对齐缺少当前批准实现文档的固定版本。')
    implementation_sources = {implementation['document_id']: implementation['sha256']}
    supplement = context.get('scope_revision_document') if partitioned else None
    if supplement:
        if supplement not in context.get('indexed_documents', []) or not context.get('approved_source_scope'):
            _fail('代码对齐缺少固定的分工修订版本。')
        implementation_sources[supplement['document_id']] = supplement['sha256']
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            _fail('代码对齐依据格式不完整。')
        requirement = item.get('requirement_id')
        if (not isinstance(requirement, str) or not requirement.strip()
                or accepted and requirement not in accepted or partitioned and requirement not in required):
            _fail('代码对齐依据引用了当前规范之外的验收 ID。')
        artifact_id, sha = item.get('artifact_id'), item.get('sha256')
        delivery = by_id.get(artifact_id)
        if not delivery or delivery.get('sha256') != sha:
            from . import code_evidence_reference_correction as references
            error = ResearchBlocked(references.ERROR)
            error.code_reference_problems = references.problems(engine, run_id, handoff_id, result, context)
            raise error
        # A missing manifest entry is itself reviewable. Only the explicit
        # same-delivery clarification may cite such a file; hashes, ownership,
        # actual same-role reads and approved-document references still apply.
        # Manifest locations guide navigation; actual dependencies and config
        # consumers elsewhere in the same frozen delivery are valid evidence.
        name = PurePosixPath(str(delivery.get('path', '')).replace('\\', '/'))
        from .review_response_policy import TEXT_SUFFIXES
        if name.suffix.lower() not in TEXT_SUFFIXES:
            from . import code_evidence_reference_correction as references
            error = ResearchBlocked(references.NON_CODE_ERROR)
            error.code_reference_problems = references.problems(engine, run_id, handoff_id, result, context)
            raise error
        offset, limit = item.get('offset'), item.get('limit')
        if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000
                or not isinstance(item.get('verification'), str) or not item['verification'].strip()):
            _fail('代码对齐依据缺少有效原文范围或具体核对说明。')
        if artifact_id not in loaded:
            loaded[artifact_id] = _code_text(engine, run_id, artifact_id, delivery)
            spans[artifact_id] = ([(start, end) for start, end, _ in partitions.delivered_spans(
                engine, run_id, role, handoff_id, context, delivery, loaded[artifact_id])] if partitioned
                else _code_spans(engine, handoff_id, artifact_id, sha, loaded[artifact_id]))
        if offset + limit > len(loaded[artifact_id]):
            _fail('代码对齐依据范围超出当前实际交付。')
        cursor = offset
        for start, end in spans[artifact_id]:
            if start <= cursor:
                cursor = max(cursor, end)
        if cursor < offset + limit:
            _fail(READ_PENDING + '；请读取 source 对应的实际代码或测试文件范围，再交付代码核对结论。')
        refs = item.get('implementation_refs')
        if supplement and result.get('approve') and not any(
                ref.get('document_id') == implementation['document_id'] and
                ref.get('document_sha256') == implementation['sha256'] for ref in refs or []):
            _fail(READ_PENDING + '；代码批准必须引用实际读取的原实现规范。')
        if supplement and result.get('approve') and not any(
                ref.get('document_id') == supplement['document_id'] and
                ref.get('document_sha256') == supplement['sha256'] for ref in refs or []):
            _fail(READ_PENDING + '；代码批准必须引用实际读取的分工修订文档。')
        try:
            ranges = _required_ranges(engine, run_id, refs, implementation_sources)
        except ResearchBlocked as exc:
            from .review_document_ids import ERRORS
            if str(exc) not in ERRORS:
                raise
            # The source may be intact while the reviewer cited a foreign
            # document or an invalid span. Only a candidate-owned locator is
            # eligible for feedback; source-load/integrity failures propagate.
            problems = []
            for ref_index, ref in enumerate(refs):
                try:
                    _required_ranges(engine, run_id, [ref], implementation_sources)
                except ResearchBlocked as problem:
                    if str(problem) not in ERRORS:
                        raise
                    problems.append({'reference_field':'implementation_refs', 'evidence_index':index,
                        'requirement_id':requirement, 'reference_index':ref_index,
                        'reference':ref, 'reason':str(problem)})
            exc.alignment_reference_problems = problems
            raise
        for did, pages in ranges.items():
            implementation_ranges.setdefault(did, []).extend(pages)
        # Manifest evidence can explain a tracking defect but cannot replace
        # reading actual implementation/tests when approving an acceptance ID.
        if name.suffix.lower() in {'.py', '.pyi'}:
            seen.add(requirement)
    if result.get('approve') is True and not set(required)<=seen:
        _fail('代码对齐批准尚未覆盖全部验收 ID 的实际实现。')
    if partitioned and result.get('approve') is True:
        from .review_response_policy import TEXT_SUFFIXES
        endpoints = {row['id']: row for row in context[partitions.FIELD]['interfaces']}
        by_path = {row['path']: row for row in deliveries}
        for finding in result['interface_findings']:
            edge = endpoints[finding['id']]
            if edge.get('provider_unresolved'):
                _fail('静态接口缺少可定位的提供端，需要在分工中明确端点：' + edge['provider'])
            path = edge['consumer'] if edge['consumer_role'] == role else edge['provider']
            delivery = by_path[path]
            artifact_id = delivery['artifact_id']
            if artifact_id not in loaded:
                loaded[artifact_id] = _code_text(engine, run_id, artifact_id, delivery)
                spans[artifact_id] = [(start, end) for start, end, _ in partitions.delivered_spans(
                    engine, run_id, role, handoff_id, context, delivery, loaded[artifact_id])]
            cursor = finding['offset']
            stop = cursor + finding['limit']
            for begin, end in spans[artifact_id]:
                if begin <= cursor:
                    cursor = max(cursor, end)
            side = 'consumer' if edge['consumer_role'] == role else 'provider'
            endpoints_for_side = edge.get(f'{side}_ranges', [])
            intersects = any(finding['offset'] < end and begin < stop for begin, end in endpoints_for_side)
            if finding['limit'] <= 0 or cursor < stop or (endpoints_for_side and not intersects):
                _fail(READ_PENDING + '；跨组端点尚未引用本角色收到的源码：' + path)
            if supplement:
                required_documents = {implementation['document_id'], supplement['document_id']}
                ranges = _required_ranges(engine, run_id, finding.get('implementation_refs'), implementation_sources)
                if set(ranges) != required_documents:
                    _fail(READ_PENDING + '；跨组端点需核对原规范及分工修订。')
                for did, pages in ranges.items():
                    implementation_ranges.setdefault(did, []).extend(pages)
        inspected = set()
        for delivery in deliveries:
            if delivery['path'] == 'implementation.json' or PurePosixPath(delivery['path']).suffix.lower() not in TEXT_SUFFIXES:
                continue
            identity = delivery['sha256']
            if identity in inspected:
                continue
            inspected.add(identity)
            artifact_id = delivery['artifact_id']
            text = loaded[artifact_id] if artifact_id in loaded else _code_text(engine, run_id, artifact_id, delivery)
            ranges = spans[artifact_id] if artifact_id in spans else partitions.delivered_spans(
                engine, run_id, role, handoff_id, context, delivery, text)
            from .document_read_window import _uncovered
            scope = context[partitions.FIELD]
            gaps = _uncovered(0, len(text), [(item[0], item[1], '') for item in ranges])
            if text and (not ranges or scope.get('complete_source_reads') and gaps):
                _fail(READ_PENDING + '；本角色尚有未核对的源码：' + delivery['path'])
    from .implementation_reading import coverage
    for did, ranges in implementation_ranges.items():
        if not coverage(engine, run_id, {'document_id': did, 'sha256': implementation_sources[did]},
                        ranges, _handoffs(engine, run_id, role, handoff_id, result, context))['complete']:
            _fail(READ_PENDING + '；请实际读取 implementation_refs 绑定的当前实现文档，再核对代码。')
    return {'verified_code_refs': len(items), 'requirement_ids': sorted(seen),
            'reading_handoff_ids': [handoff_id], 'scientific_approval': False}


@operation
def validate_vote(engine, run_id, role, handoff_id, result, context, *, delivery=False):
    """Validate the alignment contract, its source reading, and actual delivery."""
    if context.get(PROTOCOL) != 1:
        return None
    from .implementation_alignment import validate_review
    validate_review(result, context, delivery=delivery,
                    design=_workflow_design(engine, run_id, handoff_id, context),role=role)
    report = {'source_evidence': validate_review_evidence(
        engine, run_id, role, handoff_id, result, context)}
    from . import implementation_issue_responses as issue_responses
    if context.get(issue_responses.PROTOCOL) == issue_responses.VERSION:
        report['issue_confirmations'] = issue_responses.validate_review(
            engine, run_id, role, handoff_id, result, context)
    if delivery:
        report['code_evidence'] = validate_code_evidence(
            engine, run_id, role, handoff_id, result, context)
        from .code_repair_contract import validate_review as validate_repair_scope
        validate_repair_scope(result, context, role)
        from .definition_code_followup import validate_review as validate_definition_scope
        validate_definition_scope(result, context)
    return report


def validate_candidate(engine, run_id, role, handoff_id, result, context, *, delivery=False):
    """Check one unsaved final response before sealing its immutable decision.

    Only the exact current candidate may lack a completed decision record.
    Original inputs and the current immutable source versions stay verified;
    the temporary capability is scoped to this call and cannot survive errors.
    """
    if context.get(PROTOCOL) != 1:
        return None
    from .engine import digest
    token = _candidate.set((id(engine), run_id, role, handoff_id, digest(result), digest(context), result))
    try:
        from . import implementation_issue_responses as responses
        if (context.get(responses.PROTOCOL) == responses.VERSION
                and context.get('implementation_document_authoring_protocol') == 1):
            from . import implementation_authoring as author
            record = engine.store.get(author.COLLECTION, handoff_id, {})
            state = engine.store.get('codex_workflows', context[responses.CONTRACT]['workflow_id'], {})
            if role != 'engineer' or not record or record.get('workflow_id') != state.get('id'):
                _fail('作者回应候选缺少本交接实际保存的文档。')
            return responses.validate_author(engine, run_id, state, record['row'],
                handoff_id, result, context)
        # Source-pointer corrections may be recoverable, but never before the
        # candidate's role, exact frozen input and immutable version are checked.
        frozen = _delivery(engine, run_id, role, handoff_id, result)
        if digest(frozen) != digest(context):
            _fail('对齐审查上下文与原始冻结输入不一致。')
        return validate_vote(engine, run_id, role, handoff_id, result, context, delivery=delivery)
    finally:
        _candidate.reset(token)
