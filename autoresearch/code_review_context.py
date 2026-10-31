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
        'scientific_claims_verified': False, 'provenance': 'exact_frozen_handoff_field',
        'handoff_id': key, 'json_pointer': pointer}


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
    return consumed


def code_window(engine, run_id, role, key, progress, context, frozen):
    from .working_context import sha
    from .implementation_alignment_evidence import _code_text, _code_spans
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
            spans = _code_spans(engine, key, identity, ref['sha256'], text)
            end = ref['offset'] + ref['limit']
            if end > len(text):
                raise ValueError('Read correction code range exceeds frozen delivery')
            gaps = _uncovered(ref['offset'], end, [(a, b, '') for a, b in spans])
            counts['already_read_refs'] += int(not gaps)
            missing.extend({**ref, 'offset': a, 'limit': b-a} for a, b in gaps)
        correction['code_refs'] = list({_json(ref): ref for ref in missing}.values())
        counts['missing_refs'] += len(correction['code_refs'])
    correction['reading_progress'] = counts
    correction['instruction'] = ('只补读 source_refs、implementation_refs、code_refs 中列出的实际缺口。'
        '已核验读取的范围不再列出，无需为门禁重复读取；如判断确需重查仍可按原哈希回查。'
        'pending_requirement_ids 表示待完成的判断，不等于未读。缺口为空时基于原候选和已保存证据完成判断，'
        '保留未解决异议；不得把已读当作批准或把 pending 自动改为 aligned。')
    return result


@operation
def project(engine, run_id, role, key, progress, prepared):
    if not enabled(prepared['context'], prepared['context'].get('_purpose', '')):
        return prepared
    frozen = _frozen(engine, run_id, role, key)
    result = copy.deepcopy(prepared)
    context = result['context']
    archives = _archives(run_id, key, frozen)
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
    context['code_review_context_projection'] = {'version': 1, 'latest_note_document_id': latest}
    context['document_directory_archive'] = by_pointer['/context/indexed_documents']
    context['document_directory_policy'] = ('Compact navigation only; use documents.list with document_id/expected_sha256 '
        'for exact sections. Original frozen indices and old note versions remain archived; no reading credit.')
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
        context['previous_unsealed_candidate'] = {'candidate': copy.deepcopy(history[-1]['candidate']),
            'candidate_sha256': history[-1]['candidate_sha256'], 'scientific_approval': False,
            'scope': 'Prior received opinion, not an approved decision; keep unresolved findings while completing checks.'}
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
        if history and correction.get('candidate_sha256') == history[-1].get('candidate_sha256'):
            # The exact candidate already appears once in previous_unsealed_candidate.
            correction.pop('original_candidate', None)
            correction['candidate_context_path'] = 'previous_unsealed_candidate.candidate'
        repair_correction = current.get('code_repair_response_correction', {})
        if history and repair_correction.get('candidate_sha256') == history[-1].get('candidate_sha256'):
            repair_correction.pop('original_candidate', None)
            repair_correction['candidate_context_path'] = 'previous_unsealed_candidate.candidate'
        context['tool_results'].append(current)
    result['task'] += ('\n代码审查工作集：旧代码正文仅在已经消费后退出当前窗口，回查使用 code_read_reference.reopen。'
        '验收输出中的编码数组保存在同交接不可变档案，按 stdout_archive 的 artifacts.read 入口和哈希分页读取。'
        '所有错误、指标、限制与未决问题保留；目录不是已读证明。只补齐最新 reading_progress 列出的缺口。')
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
        entry = {key: value for key, value in doc.items() if key != 'sections'}
        entry['section_count'] = len(doc.get('sections', []))
        directories.append(entry)
    result['indexed_documents'] = directories
    return result
