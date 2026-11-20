"""Disposable code-review views over unchanged frozen inputs and read ledgers.

Only new requests use this projection. Archives resolve exact frozen fields;
neither a reference nor an omitted body grants source-reading credit.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .architecture_validation import operation

CODE_WINDOW_CHARS = 24000
ARCHIVE_PREFIX = ':review-evidence:'
# Static support contracts and file inventories stay available in the exact
# role-owned handoff. They need not accompany every follow-up source page.
STATIC_FIELDS = ('implementation_capability_evidence', 'code_contract',
                 'implementation_format_contract', 'delivery_artifacts')
NAVIGATION_TOOLS = {'code.locate', 'artifacts.list', 'documents.list', 'documents.search'}


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def enabled(context, purpose):
    return (purpose.split(':', 1)[0] == 'code_review'
            and context.get('implementation_alignment_protocol') == 1
            and context.get('delivery_directory_protocol') == 1)


def _frozen(engine, run_id, role, key):
    from .engine import digest
    assignment = engine.store.get('handoffs', key, {})
    frozen = engine.store.get('handoff_inputs', key, {})
    if (assignment.get('id') != key or frozen.get('id') != key
            or assignment.get('run_id') != run_id or frozen.get('run_id') != run_id
            or assignment.get('to_role') != role
            or not enabled(frozen.get('context', {}), assignment.get('purpose', ''))
            or assignment.get('input_hash') != digest({k: frozen.get(k)
                for k in ('instruction', 'context', 'schema')})):
        raise ValueError('Review evidence requires the exact frozen run, role and handoff')
    return frozen


def _archives(run_id, key, frozen):
    """Virtual artifacts backed by immutable SQLite handoff fields, not summaries."""
    fields = {'/context/indexed_documents': _json(frozen['context'].get('indexed_documents', []))}
    for name in STATIC_FIELDS:
        if name in frozen['context']:
            fields['/context/' + name] = _json(frozen['context'][name])
    for index, row in enumerate(frozen['context'].get('local_acceptance', {}).get('smoke', [])):
        for field in ('stdout', 'stderr'):
            if isinstance(row.get(field), str):
                fields[f'/context/local_acceptance/smoke/{index}/{field}'] = row[field]
    return {run_id + ARCHIVE_PREFIX + _sha(key + pointer): (pointer, text)
            for pointer, text in fields.items()}


def _locator(identity, pointer, text):
    return {'artifact_id': identity, 'sha256': _sha(text), 'total_chars': len(text),
        'storage': 'immutable_handoff_inputs', 'json_pointer': pointer,
        'reopen': {'name': 'artifacts.read', 'arguments': {
            'id': identity, 'expected_sha256': _sha(text), 'offset': 0, 'limit': 4000}}}


def read_archive(engine, run_id, role, key, args):
    frozen = _frozen(engine, run_id, role, key)
    item = _archives(run_id, key, frozen).get(args.get('id'))
    provenance = 'exact_frozen_handoff_field'
    if item is None:
        progress = engine.store.get('handoff_progress', key, {})
        consumed = _consumed(engine, run_id, role, key, progress)
        for row in progress.get('tool_results', []):
            digest = _receipt_sha(row)
            if digest in consumed and _navigation_archive_id(run_id, key, digest) == args.get('id'):
                item = ('/handoff_progress/tool_results/' + digest, _json(row))
                provenance = 'exact_consumed_navigation_receipt'
                break
    if item is None:
        raise ValueError('Review evidence archive is outside this frozen handoff')
    pointer, text = item
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    if (type(offset) is not int or not 0 <= offset <= len(text)
            or type(limit) is not int or not 1 <= limit <= 16000
            or args.get('expected_sha256') != _sha(text)):
        raise ValueError('Review evidence archive hash or range mismatch')
    end = min(len(text), offset + limit)
    return {'artifact_id': args['id'], 'sha256': _sha(text), 'text': text[offset:end],
        'offset': offset, 'total_chars': len(text), 'next_offset': end if end < len(text) else None,
        'truncated': end < len(text), 'untrusted': True, 'primary_source_reading': False,
        'scientific_claims_verified': False, 'provenance': provenance,
        'handoff_id': key, 'json_pointer': pointer}


def _receipt_sha(row):
    from .working_context import sha
    return sha(row)


def _navigation_archive_id(run_id, key, digest):
    return run_id + ARCHIVE_PREFIX + _sha(key + ':navigation:' + digest)


def _array_view(text):
    """Remove only identifiable encoded array bytes; retain all other log text.

    stdout may start inside a truncated array. Require its adjacent JSON array
    encoding metadata; never classify an arbitrary long log token as an array.
    """
    chunks = []
    def replace(match):
        tail = text[match.end():match.end() + 512].split('}', 1)[0]
        if not re.match(r'"\s*,', tail) or not re.search(r'"encoding"\s*:\s*"base64"', tail):
            return match.group()
        chunks.append({'offset': match.start(), 'limit': len(match.group()), 'sha256': _sha(match.group())})
        return f'[archived base64 array: chars={len(match.group())}; sha256={_sha(match.group())}]'
    view = re.sub(r'[A-Za-z0-9+/]{1024,}={0,2}', replace, text)
    return view, chunks


def _consumed(engine, run_id, role, key, progress):
    from .working_context import marker, validate_commit
    saved = validate_commit(engine.store, run_id, role, marker(run_id, role, key, progress))
    consumed = set()
    if saved:
        for identity in saved['payload']['receipt_ids']:
            row = engine.store.get('context_receipts', identity)
            consumed.add(row['sha256'])
            value = row['receipt'].get('result')
            reference = value.get('code_read_reference', {}) if isinstance(value, dict) else {}
            if reference.get('receipt_sha256'):
                consumed.add(reference['receipt_sha256'])
            reference = value.get('consumed_read_reference', {}) if isinstance(value, dict) else {}
            if reference.get('receipt_sha256'):
                consumed.add(reference['receipt_sha256'])
            reference = row['receipt'].get('archived_navigation', {})
            if reference.get('receipt_sha256'):
                consumed.add(reference['receipt_sha256'])
    return consumed


def fresh_document_pages(engine, run_id, role, key, progress, context):
    """Do not lose an unread batch to the older 6k-per-document prompt view.

    Retrieval and delivery remain distinct. Use actual same-role delivery
    coverage, retain bounded exact pages in the next view, and leave the
    original journal and the reading/approval gates untouched.
    """
    from .implementation_reading import coverage
    from .evidence_delivery import tracking_required
    from .document_working_notes import read_directories
    consumed = _consumed(engine, run_id, role, key, progress)
    docs = {d['document_id']: d for d in read_directories(context)
            if d.get('kind') != 'document_working_notes'}
    budget = min(72000, max(0, engine.store.get('runs', run_id).get('config', {}).get('context_tokens', 32000) // 4))
    visible = {_receipt_sha(row) for row in context.get('tool_results', [])}
    selected, chars = [], 0
    with engine.store.read_snapshot() as store:
        reader = copy.copy(engine)
        reader.store = store
        tracked = tracking_required(store, run_id, key)
        for row in reversed(progress.get('tool_results', [])):
            page = row.get('result', {})
            if (row.get('predecessor_handoff_id') is not None
                    or row.get('call', {}).get('name') != 'documents.read' or not isinstance(page, dict)
                    or page.get('error') or not isinstance(page.get('text'), str)
                    or page.get('document_id') not in docs or _receipt_sha(row) in visible):
                continue
            if chars + len(page['text']) > budget:
                continue
            if tracked:
                delivered = coverage(reader, run_id, docs[page['document_id']], [row], [key])['complete']
            else:
                delivered = _receipt_sha(row) in consumed
            if not delivered:
                selected.append(copy.deepcopy(row))
                chars += len(page['text'])
    if selected:
        context.setdefault('tool_results', []).extend(reversed(selected))
        context['code_review_fresh_document_delivery'] = {
            'pages': len(selected), 'text_chars': chars, 'maximum_chars': budget,
            'reading_credit_granted': False, 'scientific_approval': False,
            'scope': 'Exact retrieved pages still missing acknowledged delivery; protected in this disposable view.'}


def code_window(engine, run_id, role, key, progress, context, frozen):
    from .working_context import sha
    from .implementation_alignment_evidence import _code_text, _code_spans
    from .code_review_partition import FIELD as partition_field
    consumed = _consumed(engine, run_id, role, key, progress)
    deliveries = {row['artifact_id']: row for row in frozen['context'].get('delivery_artifacts', [])}
    archives = _archives(run_id, key, frozen)
    rows = context.get('tool_results', [])
    checked, used, evicted = set(), 0, 0
    for row in reversed(rows):
        call, page = row.get('call', {}), row.get('result', {})
        if call.get('name') != 'artifacts.read' or page.get('error') or not isinstance(page.get('text'), str):
            continue
        identity = call.get('arguments', {}).get('id')
        if identity in deliveries and identity not in checked:
            original = _code_text(engine, run_id, identity, deliveries[identity])
            if partition_field not in frozen['context']:
                _code_spans(engine, key, identity, deliveries[identity]['sha256'], original)
            checked.add(identity)
        elif identity in archives:
            exact = read_archive(engine, run_id, role, key, call['arguments'])
            if page != exact:
                raise ValueError('Review archive read receipt changed')
        elif identity not in checked:
            continue  # Unknown extensions/errors stay exact, never silently dropped.
        digest = sha(row)
        count = len(page['text'])
        # Fresh/uncommitted pages must be delivered at least once, even if the
        # advisory window is exceeded. Admission still enforces the same cap.
        if (context.get('code_navigation_protocol') == 1
                or context.get('_harness_control', {}).get('mode') == 'final_only'
                or digest not in consumed or used + count <= CODE_WINDOW_CHARS):
            used += count
            continue
        row['result'] = {name: copy.deepcopy(value) for name, value in page.items() if name != 'text'}
        row['result'].update(text_returned=False, code_read_reference={
            'receipt_sha256': digest, 'text_sha256': _sha(page['text']), 'text_chars': count,
            'reopen': copy.deepcopy(call), 'scope': 'Previously consumed exact receipt; original remains in handoff_progress.'})
        evicted += count
    context['code_read_window'] = {'visible_chars': used, 'evicted_chars': evicted,
        'recent_chars_target': CODE_WINDOW_CHARS, 'ledger_sha256': sha(progress.get('tool_results', [])),
        'reading_credit_granted': False, 'scope': 'Disposable view only; fresh reads and errors stay exact.'}


@operation
def missing_read_plan(engine, run_id, key, context, diagnostic):
    """Subtract authenticated same-role delivered ranges, never just retrieval."""
    # All references use one immutable SQLite view. Its compact delivery cache
    # authenticates each submitted inventory once instead of once per range.
    # The snapshot and its cache end with this calculation; no reading credit
    # or successful validation is reused across later requests.
    with engine.store.read_snapshot() as store:
        reader = copy.copy(engine)
        reader.store = store
        return _missing_read_plan(reader, run_id, key, context, diagnostic)


async def missing_read_plan_async(harness, run_id, key, context, diagnostic, progress):
    """Compute off the event loop and reject changed inputs before committing."""
    def project(progress, context, diagnostic):
        return missing_read_plan(harness.engine, run_id, key, context, diagnostic)

    return await harness._project_async(run_id, key, project,
        {'progress': progress, 'context': context, 'diagnostic': diagnostic})


def _missing_read_plan(engine, run_id, key, context, diagnostic):
    from .implementation_alignment_evidence import _required_ranges, _code_text, _code_spans
    from .implementation_reading import coverage
    from .document_read_window import _uncovered
    from . import document_index
    result = copy.deepcopy(diagnostic)
    correction = result.get('alignment_read_correction', {})
    counts = {'already_read_refs': 0, 'missing_refs': 0}
    docs = {d['document_id']: d for d in context.get('indexed_documents', [])}
    for field in ('source_refs', 'implementation_refs'):
        if field not in correction:
            continue
        missing = []
        for ref in correction[field]:
            did = ref['document_id']
            if did not in docs:
                missing.append(ref)
                continue
            required = _required_ranges(engine, run_id, [ref], {did: docs[did]['sha256']})
            report = coverage(engine, run_id, docs[did], required[did], [key])
            counts['already_read_refs'] += int(report['complete'])
            manifest, _ = document_index.load(engine, run_id, did, docs[did]['sha256'])
            sections = {s['id']: s for s in manifest['sections']}
            for gap in report['missing_ranges']:
                missing.append({**ref, 'offset': gap['start'] - sections[gap['section_id']]['start'],
                                'limit': gap['end'] - gap['start']})
        correction[field] = list({_json(ref): ref for ref in missing}.values())
        counts['missing_refs'] += len(correction[field])
    if 'code_refs' in correction:
        deliveries = {d['artifact_id']: d for d in context.get('delivery_artifacts', [])}
        missing = []
        for ref in correction['code_refs']:
            identity = ref['artifact_id']
            binding = deliveries.get(identity)
            if not binding or binding['sha256'] != ref['sha256']:
                raise ValueError('Read correction code reference is outside frozen delivery')
            text = _code_text(engine, run_id, identity, binding)
            from . import code_review_partition as partitions
            role = engine.store.get('handoffs', key, {}).get('to_role')
            spans = ([(a, b) for a, b, _ in partitions.delivered_spans(engine, run_id, role, key, context, binding, text)]
                     if partitions.FIELD in context else _code_spans(engine, key, identity, ref['sha256'], text))
            end = ref['offset'] + ref['limit']
            if end > len(text):
                raise ValueError('Read correction code range exceeds frozen delivery')
            gaps = _uncovered(ref['offset'], end, [(a, b, '') for a, b in spans])
            counts['already_read_refs'] += int(not gaps)
            missing.extend({**ref, 'offset': a, 'limit': b-a} for a, b in gaps)
        correction['code_refs'] = list({_json(ref): ref for ref in missing}.values())
        counts['missing_refs'] += len(correction['code_refs'])
    if correction.get('missing_code_evidence_ids'):
        add_evidence_targets(engine, run_id, key, context, correction)
        counts['missing_refs'] = sum(len(correction.get(k, [])) for k in ('source_refs','implementation_refs','code_refs'))
    correction['reading_progress'] = counts
    correction['instruction'] = ('只补读 source_refs、implementation_refs、code_refs 中列出的实际缺口。'
        '已核验读取的范围不再列出，无需为门禁重复读取；如判断确需重查仍可按原哈希回查。'
        'pending_requirement_ids 表示待完成的判断，不等于未读。缺口为空时基于原候选和已保存证据完成判断，'
        '保留未解决异议；不得把已读当作批准或把 pending 自动改为 aligned。')
    if correction.get('missing_code_evidence_ids'):
        correction['instruction'] += (' 本轮还缺 missing_code_evidence_ids 所列条款各自的 code_evidence。'
            'code_evidence_targets 列出冻结清单中的 Python 文件和本交接已取回的范围；阅读并核实相关内容后按对应 requirement_id 引用。'
            '范围不足时继续 code.locate、code.read；执行结果缺失时保留否决并指出缺少的宿主回执。')
    return result


def add_evidence_targets(engine, run_id, key, context, correction):
    """Give the reviewer file choices and real read ranges for each missing citation."""
    from .implementation_alignment_evidence import _code_text, _code_spans
    files = {f['path']:f for f in context.get('delivery_artifacts', []) if f['path'].endswith('.py')}
    manifest = {r['id']:r.get('locations', []) for r in context.get('implementation_alignment_manifest_requirements', [])}
    targets = []
    for requirement in correction['missing_code_evidence_ids']:
        paths = list(dict.fromkeys(location.split(':',1)[0] for location in manifest.get(requirement, [])))
        paths = [path for path in paths if path in files]
        target = {'requirement_id':requirement, 'paths':paths, 'retrieved_ranges':[]}
        first_page = None
        for path in paths:
            file = files[path]
            aid = file['artifact_id']
            text = _code_text(engine, run_id, aid, file)
            ref = {'requirement_id':requirement, 'artifact_id':aid, 'sha256':file['sha256']}
            if text and first_page is None:
                first_page = {**ref, 'offset':0, 'limit':min(len(text),2000)}
            spans = _code_spans(engine, key, aid, file['sha256'], text)
            spans = [(start, end) for start, end in spans if end > start]
            if spans:
                begin,end = spans[0]
                target['retrieved_ranges'].append({**ref, 'path':path, 'offset':begin, 'limit':min(2000,end-begin)})
                if len(target['retrieved_ranges']) == 2:
                    break
        if first_page and not target['retrieved_ranges']:
            correction.setdefault('code_refs', []).append(first_page)
        targets.append(target)
    correction['code_evidence_targets'] = targets


@operation
def project(engine, run_id, role, key, progress, prepared):
    if not enabled(prepared['context'], prepared['context'].get('_purpose', '')):
        return prepared
    frozen = _frozen(engine, run_id, role, key)
    result = copy.deepcopy(prepared)
    context = result['context']
    from .code_whole_review import restricted_view
    restricted = restricted_view(context)
    # A resumed role may already have a read diagnostic created by an older
    # host. Enrich only the next request; the paid candidate stays immutable.
    history = progress.get('alignment_candidate_history', [])
    if history and history[-1].get('diagnostic','').endswith('本次代码核对仍有 pending 项，请在同一交付上补读复核。'):
        from .code_review_outcome import missing_evidence_ids
        ids = missing_evidence_ids(history[-1]['candidate'], frozen['context'], role)
        if ids:
            plan = {'missing_code_evidence_ids':ids}
            add_evidence_targets(engine, run_id, key, frozen['context'], plan)
            plan['instruction'] = ('每一项尚缺以该 requirement_id 标记的 code_evidence；请核实所列源码与条款。'
                '可恢复本交接的读取范围，必要时定向读文件。保留实际否决和执行待办。')
            context['pending_code_evidence'] = plan
    fresh_document_pages(engine, run_id, role, key, progress, context)
    archives = {} if restricted else _archives(run_id, key, frozen)
    by_pointer = {pointer: _locator(identity, pointer, text) for identity, (pointer, text) in archives.items()}
    for index, smoke in enumerate(context.get('local_acceptance', {}).get('smoke', [])):
        for field in ('stdout', 'stderr'):
            pointer = f'/context/local_acceptance/smoke/{index}/{field}'
            if pointer not in by_pointer or not isinstance(smoke.get(field), str):
                continue
            raw = frozen['context']['local_acceptance']['smoke'][index][field]
            if smoke[field] != raw:
                raise ValueError('Frozen acceptance output changed before projection')
            view, chunks = _array_view(raw)
            if chunks:
                smoke[field] = view
                smoke[field + '_archive'] = {**by_pointer[pointer], 'array_ranges': chunks,
                    'scope': 'Only encoded array bytes externalized; all non-array log text remains exact.'}
    code_window(engine, run_id, role, key, progress, context, frozen)
    # Keep the complete host directory for validation/capture. Only the final
    # model-facing compiler view contracts it; validators must retain sections.
    latest = progress.get('document_working_notes', {}).get('document', {}).get('document_id')
    context['code_review_context_projection'] = {'version': 1, 'latest_note_document_id': latest,
        'run_id': run_id, 'role': role, 'handoff_id': key}
    if not restricted:
        context['document_directory_archive'] = by_pointer['/context/indexed_documents']
        context['code_review_static_archives'] = {
            name: by_pointer['/context/' + name] for name in STATIC_FIELDS
            if '/context/' + name in by_pointer
            and context.get(name) == frozen['context'].get(name)
            and len(_json(context[name])) > 2000}
    context['document_directory_policy'] = ('Compact navigation only; use documents.list with document_id/expected_sha256 '
        'for exact sections. Original frozen indices and old note versions remain archived; no reading credit.')
    consumed = _consumed(engine, run_id, role, key, progress)
    original_rows = {_receipt_sha(row): row for row in progress.get('tool_results', [])}
    for index, row in enumerate(context.get('tool_results', [])):
        if row.get('call', {}).get('name') not in NAVIGATION_TOOLS or set(row) - {'call', 'result'}:
            continue
        digest = _receipt_sha(row)
        if digest not in consumed or original_rows.get(digest) != row:
            continue
        text = _json(row)
        # Never hide a failed navigation or an attached warning/qualification.
        if any(re.search(r'"' + name + r'"\s*:', text) for name in
               ('error', 'errors', 'warning', 'warnings', 'issues', 'blocked', 'unknown_failure')):
            continue
        locator = _locator(_navigation_archive_id(run_id, key, digest),
                           '/handoff_progress/tool_results/' + digest, text)
        locator['storage'] = 'consumed_navigation_receipt'
        replacement = {'archived_navigation': {'receipt_sha256': digest, 'archive': locator},
                       'new_reading': False}
        if len(_json(replacement)) < len(text):
            context['tool_results'][index] = replacement
    notes = context.get('document_working_notes')
    if notes:
        entries = progress['document_working_notes']['payload']['entries']
        selected = notes['selected']
        for entry in entries:
            if entry.get('kind') == 'open_issue' and entry not in selected:
                selected.append(copy.deepcopy(entry))
        notes['omitted_notes'] = len(entries) - len(selected)
    history = progress.get('alignment_candidate_history', [])
    if history:
        from .code_reference_delivery_recovery import anchor_for
        reference_anchor = anchor_for(history)
        candidate_anchor = reference_anchor or history[-1]
        context['previous_unsealed_candidate'] = {'candidate': copy.deepcopy(candidate_anchor['candidate']),
            'candidate_sha256': candidate_anchor['candidate_sha256'], 'scientific_approval': False,
            'scope': 'Prior received opinion, not an approved decision; keep unresolved findings while completing checks.'}
        if reference_anchor:
            context['code_reference_correction_scope'] = {
                'original_candidate_sha256': reference_anchor['candidate_sha256'],
                'original_candidate_context_path': 'previous_unsealed_candidate.candidate',
                'required_evidence_count': len(reference_anchor['candidate'].get('code_evidence', [])),
                'allowed_corrections': copy.deepcopy(reference_anchor['code_reference_problems']),
                'instruction': '持续补正约束：仅修改列出的原证据索引及允许字段。全部证据条数、顺序和未列出条目、'
                    '原 approve、rationale、issues、fixes、alignment_findings、repair_findings 等其余字段原样保留。'
                    '补读不解除此约束，不得重写原结论、删除异议或合并证据；若最新答复发生误改，先恢复原集合。'}
    # Only the current correction is actionable. Older diagnostics remain in
    # the ledger; replaying all seven plans invites the same completed reads.
    correction_kinds = {'implementation_alignment_read_required', 'implementation_alignment_reference_required',
        'implementation_alignment_findings_format_required', 'implementation_routing_coordinates_required',
        'implementation_confirmation_scope_required', 'implementation_review_scope_required',
        'implementation_code_reference_required', 'implementation_code_repair_response_required'}
    latest_diagnostic = next((row for row in reversed(progress.get('tool_results', []))
        if row.get('host_diagnostic') in correction_kinds), None)
    if latest_diagnostic:
        current = (missing_read_plan(engine, run_id, key, frozen['context'], latest_diagnostic)
                   if latest_diagnostic['host_diagnostic'] == 'implementation_alignment_read_required'
                   else copy.deepcopy(latest_diagnostic))
        context['tool_results'] = [row for row in context['tool_results']
            if row.get('host_diagnostic') not in correction_kinds]
        correction = current.get('code_reference_correction', {})
        if history and correction.get('candidate_sha256') == context['previous_unsealed_candidate'].get('candidate_sha256'):
            # The exact candidate already appears once in previous_unsealed_candidate.
            correction.pop('original_candidate', None)
            correction['candidate_context_path'] = 'previous_unsealed_candidate.candidate'
        repair_correction = current.get('code_repair_response_correction', {})
        if history and repair_correction.get('candidate_sha256') == history[-1].get('candidate_sha256'):
            repair_correction.pop('original_candidate', None)
            repair_correction['candidate_context_path'] = 'previous_unsealed_candidate.candidate'
        context['tool_results'].append(current)
    if restricted:
        result['task'] += ('\n从本轮代码和文档目录按精确版本补读。'
            '目录只提供定位，源码与文档观察可按 note_id 回读。')
    else:
        result['task'] += ('\n代码审查工作集：旧代码正文仅在已经消费后退出当前窗口，回查使用 code_read_reference.reopen。'
            '验收输出中的编码数组保存在同交接不可变档案，按 stdout_archive 的 artifacts.read 入口和哈希分页读取。'
            '所有错误、指标、限制与未决问题保留；目录不是已读证明。只补齐最新 reading_progress 列出的缺口。')
        result['task'] += ('\n完整文件目录、宿主能力和格式合同在 code_review_static_archives 中提供同交接精确档案，'
            '需要时按 reopen 读取；文件定位也可直接用 code.locate。archived_read 保留旧阅读回执 SHA 和精确 reopen。'
            '档案指针不代表本次新增阅读或批准。')
    from .code_repair_response_correction import request_mode
    correction_mode = request_mode(progress)
    if correction_mode:
        context['code_review_correction_mode'] = correction_mode
    from .review_response_policy import FIELD as POLICY, VERSION, GUIDANCE, acceptance_schema
    context[POLICY] = VERSION
    result['schema'] = acceptance_schema(result['schema'])
    result['task'] += '\n' + GUIDANCE
    # The later full-review window owns raw pages and may replace tool_results.
    # Carry a host-authenticated plan to the compiler rather than relying on
    # an early disposable consumed marker surviving that replacement.
    from .code_review_reading import (PLAN, REUSE_POLICY, REUSE_POLICY_VERSION,
        compiler_enabled, prepare as reading_plan)
    plan = (reading_plan(engine, run_id, role, key, frozen['context'], progress)
            if compiler_enabled(engine, run_id, context) else None)
    if plan is not None:
        context[PLAN] = plan
        context['code_review_context_projection'][REUSE_POLICY] = REUSE_POLICY_VERSION
        from .code_review_reread import project as reread_request
        result = reread_request(result, progress)
    return result


def directory_view(context):
    """Navigation-only compiler transform; host prepared evidence stays exact."""
    marker = context.get('code_review_context_projection', {})
    if marker.get('version') != 1 or not enabled(context, context.get('_purpose', '')):
        return context
    result = copy.deepcopy(context)
    latest = marker.get('latest_note_document_id')
    directories = []
    for doc in result.get('indexed_documents', []):
        if doc.get('kind') == 'document_working_notes' and doc.get('document_id') != latest:
            continue
        # Preserve every unknown field and qualifier. Only the bulky section
        # navigation leaves the prompt; documents.list restores it exactly.
        entry = {key: value for key, value in doc.items() if key not in {'sections', 'read', 'search'}}
        entry['section_count'] = len(doc.get('sections', []))
        directories.append(entry)
    result['indexed_documents'] = directories
    for name, archive in result.get('code_review_static_archives', {}).items():
        if name in STATIC_FIELDS and name in result:
            result[name] = {'available_on_demand': True, 'archive': copy.deepcopy(archive),
                'reading_credit_granted': False}
    # The host prepared request and its journals retain the full receipts. The
    # final wire view need not repeat each archived read's identity three times
    # (call, result, reopen). Fresh text, errors and unknown qualifiers stay exact.
    navigation = {'artifact_id', 'document_id', 'sha256', 'path', 'section_id',
        'char_start', 'char_end', 'total_chars', 'offset', 'limit', 'next_offset',
        'truncated', 'text_returned', 'receipt_sha256', 'text_chars', 'text_sha256',
        'consumed_read_reference', 'code_read_reference', 'document_read_reference'}
    for index, row in enumerate(result.get('tool_results', [])):
        page = row.get('result', {})
        if (set(row) - {'call', 'result'} or not isinstance(page, dict) or page.get('error')
                or 'text' in page or page.get('text_returned') is not False
                or row.get('call', {}).get('name') not in {'artifacts.read', 'code.read', 'documents.read'}):
            continue
        ref = next((page[k] for k in ('consumed_read_reference', 'code_read_reference', 'document_read_reference')
                    if isinstance(page.get(k), dict)), {})
        if (not re.fullmatch('[0-9a-f]{64}', str(ref.get('receipt_sha256', '')))
                or ref.get('reopen') != row.get('call')):
            continue
        compact = {'archived_read': {'receipt_sha256': ref['receipt_sha256'],
                                    'reopen': copy.deepcopy(ref['reopen'])},
                   'new_reading': False}
        qualifiers = {k: v for k, v in page.items() if k not in navigation}
        if qualifiers:
            compact['qualifiers'] = qualifiers
        if len(_json(compact)) < len(_json(row)):
            result['tool_results'][index] = compact
    return result
