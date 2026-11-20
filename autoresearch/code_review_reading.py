"""Disposable, delivery-authenticated reading inventory for whole code reviews.

Acquisition, working commits and acknowledged source delivery are distinct.
This module writes none of them. Only a newly prepared compiler input carries
the plan; paid requests and their original journals are never revised.
"""
from __future__ import annotations

import copy
import hashlib
from collections import Counter

from .working_context import sha, marker, validate_commit, receipt_id
from .architecture_validation import operation

PLAN = '_code_review_reading_plan'
INDEX = 'code_review_reading_inventory'
VERSION = 1
REUSE_POLICY = 'reading_reuse_policy_version'
REUSE_POLICY_VERSION = 1
FULL_PROJECTION = 'full_review_reading_projection'
TOOLS = {'code.read', 'artifacts.read', 'documents.read', 'reviews.read_material'}
PAGE_FIELDS = {
    'artifact_id', 'document_id', 'sha256', 'path', 'section_id', 'title',
    'document_version', 'evidence_state', 'text', 'char_start', 'char_end',
    'offset', 'limit', 'end', 'total_chars', 'next_offset', 'truncated',
    'primary_source_reading', 'scientific_claims_verified', 'untrusted',
    'provenance', 'start_line', 'end_line', 'receipt_sha256', 'policy_sha256',
    'material_sha256', 'max_chars', 'batch_max_chars', 'read_with', 'reading_only',
    'material_id', 'protocol', 'materials', 'required_total_chars',
}
VIEWS = {'archived_source_fields', 'text_returned', 'code_read_reference',
         'consumed_read_reference', 'document_read_reference'}


def compiler_enabled(engine, run_id, context):
    """Never leak private queued bytes into a legacy noncompiler transport."""
    version = engine.store.get('runs', run_id, {}).get('config', {}).get('context_compiler_version')
    return (type(version) is int and version in (1, 2, 3)
            or version is None and bool(context.get('_context_assembly')))


def reuse_policy(run_id, active, *, authenticated_plan):
    """A versioned clarification for new projections, never old paid inputs.

    An inventory alone is not authority. The compiler must have consumed the
    exact producer plan in this build, including its delivered-source checks.
    """
    context = active.get('context', {})
    owner = _owner(context)
    if REUSE_POLICY not in owner:
        return None
    if type(owner[REUSE_POLICY]) is not int or owner[REUSE_POLICY] != REUSE_POLICY_VERSION:
        raise ValueError('Unsupported code-review reading reuse policy version')
    if not authenticated_plan:
        return None  # Never infer prior delivery from a copied/claimed index.
    index = context.get(INDEX, {})
    if (('code_review_context_projection' in context
            and context.get('_purpose', '').split(':', 1)[0] != 'code_review')
            or type(index.get('version')) is not int or index.get('version') != VERSION
            or owner.get('run_id') != run_id or index.get('run_id') != run_id
            or any(owner.get(k) != index.get(k) for k in ('role', 'handoff_id'))
            or active.get('role', owner.get('role')) != owner.get('role')
            or index.get('scientific_approval') is not False
            or index.get('reading_credit_granted') is not False):
        raise ValueError('Code-review reading reuse policy lacks its authenticated owner/inventory')
    return ('Fresh primary pages remain exact or source-validated anchored cards. '
        'Preserve failures, conditions, contrary evidence and unresolved judgments. '
        'For this same-role, same-handoff review, source ranges covered by '
        'code_review_reading_inventory.sources[*].delivered_ranges were previously delivered as exact text. '
        'You may reuse your own saved observations anchored to those exact source versions and ranges. '
        'A source body leaving the current window does not revoke its authenticated earlier delivery; '
        'reviewed bodies need not all appear simultaneously in this input. '
        'Expand only the decisive missing ranges when your retained observations lack needed details, '
        'sources conflict, a version changes, or an exact quotation is needed. '
        'An inventory or note alone never proves comprehension, approval or execution. '
        'Do not inherit another role\'s reading or conclusions, infer unknown details, '
        'or replace missing execution receipts with repeated source reading. '
        'Configured/provider capacity remains the hard input limit. '
        'All source, reading, consensus and execution gates remain unchanged.')


def _owner(context):
    if 'code_review_context_projection' in context:
        return context['code_review_context_projection']
    owner = context.get(FULL_PROJECTION, {})
    if owner:
        from . import independent_full_review as full, review_material_packet as packets
        if (type(owner.get('version')) is not int or owner['version'] != 1
                or type(owner.get('material_protocol')) is not int
                or owner['material_protocol'] not in (2, 3)
                or context.get('review_material', {}).get('protocol') != owner['material_protocol']
                or owner.get('policy_sha256') != context.get(full.KEY, {}).get('sha256')):
            raise ValueError('Full-review reading projection lacks its exact supported source policy')
    return owner


def _fingerprint(row):
    """Ignore only host body-window fields, never qualifications/extensions."""
    if not isinstance(row, dict) or set(row) != {'call', 'result'}:
        return None
    call, page = row['call'], row['result']
    if (not isinstance(call, dict) or call.get('name') not in TOOLS
            or not isinstance(page, dict) or set(page) - PAGE_FIELDS - VIEWS):
        return None
    for name in VIEWS - {'text_returned'}:
        if name in page and not isinstance(page[name], dict):
            return None
    archive = page.get('archived_source_fields', {}).get('text', {})
    if not isinstance(archive, dict):
        return None
    text = page.get('text')
    text_sha = (hashlib.sha256(text.encode()).hexdigest() if isinstance(text, str)
                else archive.get('sha256'))
    if text_sha is None:
        text_sha = next((page[name].get('text_sha256') for name in
            ('code_read_reference', 'document_read_reference', 'consumed_read_reference')
            if isinstance(page.get(name), dict) and page[name].get('text_sha256')), None)
    if text_sha is None:
        return None
    return sha({'call': call, 'text_sha256': text_sha,
                'receipt_sha256': page.get('receipt_sha256'),
                'result': {k: v for k, v in page.items()
                if k not in VIEWS | {'text', 'receipt_sha256'}}})


def _merge(spans):
    result = []
    for start, end in sorted(set(spans)):
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def _exact_entries(entries):
    """Fold identical typed catalog objects, never conflicting metadata."""
    from .context_compiler import encoded
    return list({encoded(entry): entry for entry in entries}.values())


def _source(engine, run_id, frozen, row, cache):
    """Resolve exact catalog identity and bytes, with no implicit SHA aliases."""
    from . import independent_full_review as full, document_index
    from .code_navigation import body
    call, page = row['call'], row['result']
    args, tool = call.get('arguments', {}), call['name']
    if tool == full.TOOL:
        # The public material reader authenticates both protocol versions.
        reader_key = ('material_reader',)
        if reader_key not in cache:
            cache[reader_key] = full.MaterialReader(frozen)
        reader = cache[reader_key]
        exact = reader.read(args)
        if sha(exact) != sha(page):
            return None
        key = ('material', page['document_id'], page['sha256'])
        if key not in cache:
            cache[key] = reader.text(args.get('material_id'))
        return key, cache[key], page['char_start'], page['char_end']
    if tool == 'documents.read':
        docs = [d for d in frozen.get('indexed_documents', [])
                if d.get('document_id') == page.get('document_id')
                and d.get('sha256') == page.get('sha256')]
        # Identical catalog entries can occur through two frozen producers.
        # Compare complete, strictly encoded objects; conflicting metadata or
        # sections still prevent admission. Never rewrite the frozen catalog.
        docs = _exact_entries(docs)
        if len(docs) != 1 or docs[0].get('kind') == 'document_working_notes':
            return None
        key = ('document', page['document_id'], page['sha256'])
        if key not in cache:
            cache[key] = document_index.load(engine, run_id, key[1], key[2])[1]
        if sha(document_index.read(engine, run_id, args)) != sha(page):
            return None
        return key, cache[key], page['char_start'], page['char_end']
    files = [s for s in frozen.get('delivery_artifacts', [])
             if s.get('artifact_id') == page.get('artifact_id')
             and s.get('sha256') == page.get('sha256')]
    files = _exact_entries(files)
    if len(files) != 1:
        return None
    source = files[0]
    expected = args.get('expected_sha256')
    expected_ok = (expected == source['sha256'] if tool == 'code.read' else
                   'expected_sha256' not in args or isinstance(expected, str) and expected.lower() == source['sha256'])
    if (args.get('id') != source['artifact_id'] or not expected_ok
            or page.get('path', source['path']) != source['path']):
        return None
    key = ('artifact', source['artifact_id'], source['sha256'])
    if key not in cache:
        cache[key] = body(engine, run_id, frozen, source)
    text = cache[key]
    start, limit = args.get('offset', 0), args.get('limit', 4000 if tool == 'code.read' else 16000)
    if (set(args) - {'id', 'expected_sha256', 'offset', 'limit'}
            or type(start) is not int or type(limit) is not int or start < 0 or limit < 1
            or limit > (6000 if tool == 'code.read' else 16000)
            or type(page.get('offset')) is not int or page.get('offset') != start
            or type(page.get('total_chars')) is not int or page.get('total_chars') != len(text)):
        return None
    end = min(len(text), start + limit)
    canonical = {'artifact_id': key[1], 'sha256': key[2], 'text': text[start:end],
        'offset': start, 'total_chars': len(text),
        'next_offset': end if end < len(text) else None, 'truncated': end < len(text)}
    if tool == 'code.read':
        from .code_navigation import coordinates
        from .code_repair_contract import sha as code_receipt_sha
        canonical.update(path=source['path'], untrusted=True, **coordinates(text, start, end))
        canonical['receipt_sha256'] = code_receipt_sha({'call': call, 'result': canonical})
    if page != canonical or any(type(page[k]) is not type(v) for k, v in canonical.items()):
        return None
    return key, text, start, end


def _delivered(store, run_id, role, hid, key, text):
    """Small request scopes avoid repeatedly authenticating the whole history.

    Candidate receipts only select requests to inspect. Every admitted span
    still comes from delivery_spans' source/manifest/submitted-input checks.
    Invalid first choices cannot mask a later authentic delivery.
    """
    from .evidence_delivery import delivery_spans, covering_receipts
    identity_key = 'artifact_id' if key[0] == 'artifact' else 'document_id'
    if hasattr(store, 'record_keys'):
        from .storage import _DELIVERY_HANDOFF
        keys = store.record_keys('evidence_deliveries', run_id,
            predicate="json_extract(data,'$.role')=? AND (" + _DELIVERY_HANDOFF + ")=? AND "
                "json_extract(data,'$." + identity_key + "')=?",
            parameters=[role, hid, key[1]])
        candidates = [store.get('evidence_deliveries', identity) for identity in keys]
    else:
        candidates = store.list('evidence_deliveries', run_id)
    possible = []
    for row in candidates:
        start, end = row.get('char_start'), row.get('char_end')
        if (row.get('run_id', row.get('research_id')) == run_id and row.get('role') == role
                and row.get('handoff_id', row.get('operation_id')) == hid
                and row.get(identity_key) == key[1] and row.get('sha256', row.get('text_sha256')) == key[2]
                and row.get('status') == 'delivered' and row.get('request_id')
                and type(start) is int and type(end) is int and 0 <= start < end <= len(text)
                and row.get('page_sha256') == hashlib.sha256(text[start:end].encode()).hexdigest()):
            possible.append(row)
    possible.sort(key=lambda row: (-(row['char_end'] - row['char_start']),
                                  row.get('created_at', ''), row['request_id'], row.get('id', '')))
    verified, checked = [], set()
    for row in possible:
        if covering_receipts(verified, row['char_start'], row['char_end']):
            continue
        request = row['request_id']
        if request not in checked:
            checked.add(request)
            verified.extend(delivery_spans(store, run_id, role, key[1], key[2],
                identity_key=identity_key, handoff_id=hid, request_ids=[request], text=text))
    return verified


@operation
def prepare(engine, run_id, role, hid, frozen, progress):
    """Authenticate once in one read snapshot; return an ephemeral wire plan."""
    from . import independent_full_review as full
    if not full.tool_available(frozen):
        return None
    from .code_whole_review import permits_tool
    restricted = full.code_delivery_scope(frozen)
    from .evidence_delivery import covering_receipts
    with engine.store.read_snapshot() as store:
        reader = copy.copy(engine)
        reader.store = store
        saved = validate_commit(store, run_id, role, marker(run_id, role, hid, progress))
        # Counts identify *new occurrences*, not proof of delivered reading.
        old = Counter()
        if saved:
            for identity in saved['payload']['receipt_ids']:
                fingerprint = _fingerprint(store.get('context_receipts', identity)['receipt'])
                if fingerprint:
                    old[fingerprint] += 1
        seen, replaceable, pending, sources, cache, receipts = Counter(), Counter(), {}, {}, {}, {}
        from .full_review_window import requested_indices
        requested = set(requested_indices(progress, progress.get('tool_results', [])))
        for position, row in enumerate(progress.get('tool_results', [])):
            fingerprint = _fingerprint(row)
            if not fingerprint or not isinstance(row['result'].get('text'), str) or not row['result']['text']:
                continue
            if restricted and not permits_tool(frozen, row['call']['name'], row['call'].get('arguments', {})):
                continue
            seen[fingerprint] += 1
            try:
                source = _source(reader, run_id, frozen, row, cache)
            except (ValueError, OSError, KeyError, TypeError, UnicodeError):
                continue  # Unverifiable or extended receipts stay exact.
            if not source:
                continue
            key, text, start, end = source
            page = row['result']
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                    or hashlib.sha256(text.encode()).hexdigest() != key[2]
                    or text[start:end] != page['text']):
                continue
            if key not in receipts:
                receipts[key] = _delivered(store, run_id, role, hid, key, text)
            delivered = bool(covering_receipts(receipts[key], start, end))
            fresh = seen[fingerprint] > old[fingerprint] if saved else position in requested
            replaceable[fingerprint] += 1
            item = sources.setdefault(key, {'kind': key[0], 'id': key[1], 'sha256': key[2],
                'delivered_ranges': [], 'acquired_ranges': [], 'reopen': copy.deepcopy(row['call'])})
            item['acquired_ranges'].append((start, end))
            if delivered:
                item['delivered_ranges'].append((start, end))
            if fresh or not delivered:
                # Keep each explicit call once. Old identical acquisitions do
                # not create an endless pending queue, and aliases earn no
                # duplicate range credit. A new identical occurrence is fresh.
                pending[fingerprint] = {'row': {**copy.deepcopy(row), 'result': {
                    k: copy.deepcopy(v) for k, v in page.items() if k != 'text'}},
                    'source_bytes': page['text'], 'reason': 'new_occurrence' if fresh else 'not_delivered',
                    'requested_batch': position in requested}
        for item in sources.values():
            for field in ('delivered_ranges', 'acquired_ranges'):
                item[field] = _merge(item[field])
        value = {'version': VERSION, 'run_id': run_id, 'role': role, 'handoff_id': hid,
            'replaceable': dict(replaceable),
            'pending': sorted(pending.values(), key=lambda p: not p['requested_batch']),
            'sources': list(sources.values())}
        return {**value, 'sha256': sha(value)}


def _body_chars(value):
    if isinstance(value, dict):
        return sum(len(v) if k in {'text', 'excerpt', 'stdout', 'stderr', 'conclusion', 'statement'} and isinstance(v, str)
                   else sum(len(q) if isinstance(q, str) else _body_chars(q) for q in v)
                   if k == 'open_questions' and isinstance(v, list)
                   else _body_chars(v) for k, v in value.items())
    if isinstance(value, list):
        return sum(map(_body_chars, value))
    return 0


def apply(run_id, active):
    """Run after the full-review window, before compiler packing/selection.

    Restores selected acquired-only/new-occurrence pages from exact journal
    bytes. The combined window still obeys 50k; deferred pages remain pending
    and are offered automatically on a later newly compiled request.
    """
    from .independent_full_review import READ_LIMIT
    context = active.get('context', {})
    plan = context.get(PLAN)
    if not plan:
        return active, set()
    payload = {k: v for k, v in plan.items() if k != 'sha256'}
    projection = _owner(context)
    working = context.get('_working_set', {})
    if (plan.get('version') != VERSION or type(plan.get('version')) is not int
            or plan.get('run_id') != run_id or plan.get('role') != active.get('role', projection.get('role'))
            or any(projection.get(k) != plan.get(k) for k in ('run_id', 'role', 'handoff_id'))
            or working and (working.get('handoff_id') != plan.get('handoff_id')
                            or working.get('role') != plan.get('role'))
            or plan.get('sha256') != sha(payload)):
        raise ValueError('Code review reading projection identity/integrity mismatch')
    result = copy.deepcopy(active)
    context = result['context']
    context.pop(PLAN)
    budget = Counter(plan['replaceable'])
    rows = []
    removed = 0
    for row in context.get('tool_results', []):
        fingerprint = _fingerprint(row)
        if fingerprint and budget[fingerprint] > 0:
            budget[fingerprint] -= 1
            removed += 1
        else:
            rows.append(row)
    context['tool_results'] = rows
    used = _body_chars(context)
    selected, deferred, protected = [], [], set()
    for pending in plan['pending']:
        row = copy.deepcopy(pending['row'])
        text = pending['source_bytes']
        if used + len(text) <= READ_LIMIT:
            row['result']['text'] = text
            rows.append(row)
            protected.add(receipt_id(run_id, row))
            used += len(text)
            selected.append(row)
        else:
            deferred.append({'reopen': row['call'], 'reason': pending['reason'],
                'chars': len(text), 'delivered_in_this_request': False,
                'deferred_reason': 'insufficient_remaining_source_window'})
    context[INDEX] = {'version': VERSION, 'run_id': run_id, 'role': plan['role'],
        'handoff_id': plan['handoff_id'], 'sources': copy.deepcopy(plan['sources']),
        'historical_read_rows_consolidated': removed, 'fresh_pages_visible': len(selected),
        'remaining_source_chars': max(0, READ_LIMIT - used),
        'pending_delivery': deferred, 'reading_credit_granted': False, 'scientific_approval': False,
        'guidance': 'delivered_ranges 仅指同角色、同交接、同版本原文已投递，不代表理解或批准。'
            'acquired_ranges 仅指工具取回。旧回执保留在原账本；按 reopen 精确回查。'
            'pending_delivery 正文尚未投递，后续窗口按剩余容量自动补送。'
            '若持续放不下，可按 reopen 缩小 limit 至 remaining_source_chars 内定向补读实际缺口；'
            '剩余为0时明确保留容量不足和未知项，不得宣称已读或通过。'
            '同版本已读范围和已保存笔记优先复用；新读取应对应具体未知字段、矛盾或待核对位置。'
            '必要时仍可重查；重读次数不构成进度。证据足够时保存原异议并提交独立意见，'
            '缺少执行回执必须明确保留，不能用继续读相同代码代替执行证据。'}
    context['full_review_source_window'] = {'version': 1, 'visible_source_chars': used,
        'maximum_chars': READ_LIMIT, 'archived_receipts_preserved': True}
    return result, protected
