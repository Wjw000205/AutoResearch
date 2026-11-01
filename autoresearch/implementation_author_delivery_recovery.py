"""One final-only continuation when the host clipped already retrieved evidence.

This repairs delivery, not reading, document content, or scientific approval.
The original paid packet and the exact tool ledger stay immutable.
"""
from __future__ import annotations

import copy
import hashlib

from . import implementation_author_read_recovery as reading
from . import implementation_authoring as author
from .implementation_author_recovery_scope import SCOPE_VERSION
from .implementation_review_routing import digest
from .storage import now, uid

KEY = 'implementation_author_delivery_recovery'


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现作者证据交付恢复：' + message)


def _scope(h, run_id, role, purpose, hid, progress):
    reading.validate_scope(h, run_id, role, purpose, hid, progress)
    base, record, context = reading._scope(h.engine, run_id, role, purpose, hid, progress)
    if (progress.get(reading.KEY) is None or progress.get('pending_tool_calls')
            or h.engine.store.get('runs', run_id, {}).get('status') != 'running'):
        _fail('原补读范围或活动状态已改变。')
    return {**base, 'normal_request_sha256': digest(progress.get('normal_request')),
        'tool_results_sha256': digest(progress.get('tool_results')),
        'read_grant_sha256': digest(progress[reading.KEY]), 'turn': progress['turn'],
        'save_grant_sha256': digest(progress.get('implementation_document_save_resume'))}, record, context


def _retrieved(engine, run_id, progress, document, ranges):
    """Authenticate raw reads without turning retrieval into delivery credit."""
    from .document_index import load
    manifest, text = load(engine, run_id, document['document_id'], document['sha256'])
    sections = {row['id']: row for row in manifest['sections']}
    spans = {}
    for receipt in progress.get('tool_results', []):
        if not isinstance(receipt, dict) or receipt.get('predecessor_handoff_id') is not None:
            continue
        call, page = receipt.get('call', {}), receipt.get('result', {})
        args = call.get('arguments', {})
        if call.get('name') != 'documents.read' or args.get('document_id') != document['document_id']:
            continue
        if isinstance(page, dict) and page.get('error'):
            continue
        if not isinstance(page, dict):
            _fail('原工具正文回执损坏。')
        section = sections.get(args.get('section_id'))
        start, end = page.get('char_start'), page.get('char_end')
        offset, limit = args.get('offset'), args.get('limit')
        if (not section or type(start) is not int or type(end) is not int
                or type(offset) is not int or type(limit) is not int or not 1 <= limit <= 6000
                or not section['start'] <= start < end <= section['end']
                or start != section['start'] + offset or end > start + limit
                or args.get('expected_sha256') != document['sha256']
                or page.get('document_id') != document['document_id'] or page.get('sha256') != document['sha256']
                or page.get('section_id') != section['id'] or page.get('text') != text[start:end]):
            _fail('原工具正文、范围或版本不一致，不能通过重新交付掩盖。')
        spans.setdefault(section['id'], []).append((start, end))
    for required in ranges:
        cursor = required['start']
        for start, end in sorted(spans.get(required['section_id'], [])):
            if start > cursor:
                break
            cursor = max(cursor, end)
            if cursor >= required['end']:
                break
        if cursor < required['end']:
            _fail('仍有未实际取回的原文，不能领取无工具交付恢复。')


def _subset(references, allowed):
    for ref in references:
        cursor = ref['start']
        for row in sorted((x for x in allowed if x['section_id'] == ref['section_id']), key=lambda x: x['start']):
            if row['start'] > cursor:
                break
            cursor = max(cursor, row['end'])
            if cursor >= ref['end']:
                break
        if cursor < ref['end']:
            _fail('候选增加了原补读范围外的当前稿证据，未扩大恢复。')


def _indexed_clipped(original, ledger, document, missing):
    """Authenticate a compact archive without confusing its index with delivery."""
    from .document_read_index import archived_rows
    from .document_read_window import _reference, _uncovered, sha
    try:
        archived = archived_rows(original, ledger)
    except ValueError as exc:
        _fail('原请求阅读索引不能认证：' + str(exc))
    count = original['document_read_window']['read_index']['ledger_receipts']
    raw_hashes = {sha(row) for row in ledger[:count]}
    visible = {}
    for row in original.get('tool_results', []):
        if not isinstance(row, dict) or row.get('call', {}).get('name') != 'documents.read':
            continue
        page = row.get('result', {})
        if (not isinstance(page, dict) or page.get('document_id') != document['document_id']
                or page.get('sha256') != document['sha256'] or page.get('error')):
            continue
        if 'text' in page:
            if sha(row) not in raw_hashes:
                _fail('原请求可见正文不属于认证的原回执。')
            visible.setdefault(page['section_id'], []).append((page['char_start'], page['char_end'], None))
            continue
        ref = page.get('document_read_reference', {})
        position = ref.get('ledger_position') if isinstance(ref, dict) else None
        if type(position) is not int or not 0 <= position < count:
            _fail('原请求窗口片段缺少认证的原回执位置。')
        actual = ledger[position]
        expected = _reference(actual, position)
        expected['result']['document_read_fragments'] = page.get('document_read_fragments', [])
        supplied = copy.deepcopy(row)
        supplied['result'].setdefault('document_read_fragments', [])
        if supplied != expected or not isinstance(page.get('document_read_fragments', []), list):
            _fail('原请求窗口片段的父回执已改变。')
        for part in page.get('document_read_fragments', []):
            if not isinstance(part, dict):
                _fail('原请求窗口片段损坏。')
            a, b = part.get('char_start'), part.get('char_end')
            if (type(a) is not int or type(b) is not int
                    or not page['char_start'] <= a < b <= page['char_end']
                    or any(part.get(k) != page[k] for k in ('document_id', 'sha256', 'section_id'))
                    or part.get('text') != actual['result']['text'][a-page['char_start']:b-page['char_start']]
                    or part.get('text_sha256') != hashlib.sha256(part['text'].encode('utf-8')).hexdigest()):
                _fail('原请求窗口片段与原回执不一致。')
            visible.setdefault(page['section_id'], []).append((a, b, None))
    for _, row in archived:
        page = row['result']
        if any(page.get(k) != document[k] for k in ('document_id', 'sha256')):
            continue
        for required in missing:
            if required['section_id'] != page['section_id']:
                continue
            a, b = max(required['start'], page['char_start']), min(required['end'], page['char_end'])
            if a < b and _uncovered(a, b, visible.get(page['section_id'], [])):
                return True
    return False


def _clipped(source, progress, document, missing):
    """Prove clipping against the actual original ledger, not a model marker."""
    from .document_read_window import _reference
    original = source['prepared']['context']
    if 'read_index' in original.get('document_read_window', {}):
        return _indexed_clipped(original, progress['tool_results'], document, missing)
    if original.get('document_read_window', {}).get('version') != 2:
        return False
    ledger = progress['tool_results']
    for row in original.get('tool_results', []):
        if not isinstance(row, dict) or row.get('call', {}).get('name') != 'documents.read':
            continue
        page = row.get('result', {})
        if (not isinstance(page, dict) or page.get('document_id') != document['document_id']
                or page.get('sha256') != document['sha256']):
            continue
        ref = page.get('document_read_reference', {})
        if not isinstance(ref, dict):
            _fail('原请求引用回执损坏。')
        position = ref.get('ledger_position')
        if type(position) is not int or not 0 <= position < len(ledger):
            continue
        actual = ledger[position]
        if (row['call'] != actual.get('call') or ref != _reference(actual, position)['result']['document_read_reference']
                or any(page.get(key) != actual['result'].get(key)
                       for key in ('document_id', 'sha256', 'section_id', 'char_start', 'char_end'))):
            continue
        if 'text' in page or page.get('text_returned') is not False:
            _fail('原请求引用仍包含正文，不能当作宿主裁剪。')
        fragments = page.get('document_read_fragments', [])
        if not isinstance(fragments, list):
            _fail('原请求窗口片段账本损坏。')
        for part in fragments:
            if not isinstance(part, dict):
                _fail('原请求窗口片段损坏。')
            a, b = part.get('char_start'), part.get('char_end')
            if (type(a) is not int or type(b) is not int
                    or not page['char_start'] <= a < b <= page['char_end']
                    or any(part.get(key) != page[key] for key in ('document_id', 'sha256', 'section_id'))
                    or part.get('text') != actual['result']['text'][a-page['char_start']:b-page['char_start']]
                    or part.get('text_sha256') != hashlib.sha256(part['text'].encode('utf-8')).hexdigest()):
                _fail('原请求窗口片段与原回执不一致。')
        for required in missing:
            if required['section_id'] != page['section_id']:
                continue
            start, end = max(required['start'], page['char_start']), min(required['end'], page['char_end'])
            if start >= end:
                continue
            cursor = start
            for part in sorted(fragments, key=lambda p: p['char_start']):
                a, b = part.get('char_start'), part.get('char_end')
                if a > cursor:
                    break
                cursor = max(cursor, b)
            if cursor < end:
                return True
    return False


def _source(h, run_id, audit):
    from .execution_archive import load
    from .implementation_save_resume import _settled_response
    try:
        source = load(h.engine.store, run_id, 'engineer', audit['source_archive_id'],
                      audit['source_archive_sha256'])['request']
    except (KeyError, ValueError, OSError) as exc:
        _fail('原答复归档无法校验：' + str(exc))
    if (digest(source) != audit['source_request_sha256'] or source.get('status') != 'response_saved'
            or source.get('operation_id') != audit['source_operation_id']
            or digest(source.get('response', {}).get('json')) != audit['candidate_sha256']
            or not _settled_response(h.engine.store, run_id, source)):
        _fail('原完整付费答复或归档已改变，未重发原请求。')
    return source


def _checked(h, run_id, role, purpose, hid, progress):
    audit = progress.get(KEY)
    if audit is None:
        return None
    if (not isinstance(audit, dict) or type(audit.get('version')) is not int or audit['version'] != 1
            or audit.get('scope_version') != SCOPE_VERSION or audit.get('run_id') != run_id
            or audit.get('role') != role or audit.get('purpose') != purpose or audit.get('handoff_id') != hid
            or audit.get('attempt') != 1 or audit.get('tools_allowed') is not False
            or audit.get('scientific_approval') is not False
            or audit.get('sha256') != digest({k: v for k, v in audit.items() if k != 'sha256'})
            or progress.get('mode') != 'final_only' or _scope(h, run_id, role, purpose, hid, progress)[0] != audit.get('scope')):
        _fail('唯一交付恢复的输入、工具账本、版本或额度已改变。')
    source = _source(h, run_id, audit)
    from .implementation_author_response_correction import KEY as FORMAT_KEY
    correction = progress.get(FORMAT_KEY)
    if correction is not None:
        # The ordinary once-only declaration correction may follow delivery.
        # Its own validator authenticates this archived final and new packet.
        if correction.get('source_operation_id') != audit['operation_id']:
            _fail('字段补正未绑定这次实际交付收尾。')
        from .implementation_author_response_recovery import validate_preview
        validate_preview(h, run_id, role, purpose, hid, progress)
    else:
        active = progress.get('finalization', {})
        if (active.get('operation_id') != audit['operation_id']
                or digest(active.get('prepared')) != audit['prepared_sha256']
                or active.get('author_delivery_of') != source['operation_id']
                or active.get('withheld_tool_calls')
                or active.get('completed_result_hashes') != [digest(row) for row in progress.get('tool_results', [])]):
            _fail('交付收尾请求已替换或添加了工具。')
    _retrieved(h.engine, run_id, progress, progress[reading.KEY]['document'], progress[reading.KEY]['missing_ranges'])
    return audit, source


def validate_preview(h, run_id, role, purpose, hid, progress):
    _checked(h, run_id, role, purpose, hid, progress)


def validate_dispatch(h, run_id, hid, progress):
    assignment = h.engine.store.get('handoffs', hid, {})
    checked = _checked(h, run_id, assignment.get('to_role'), assignment.get('purpose', ''), hid, progress)
    if checked is not None and hasattr(h.intelligence, 'provider_for'):
        _, source = checked
        provider = h.intelligence.provider_for('engineer')
        if (source.get('provider_id'), source.get('model')) != (provider.get('id'), provider.get('model')):
            _fail('必须沿用原工程师供应方和模型。')


def validate_candidate(h, run_id, role, purpose, hid, result):
    checked = _checked(h, run_id, role, purpose, hid, h.engine.store.get('handoff_progress', hid, {}))
    if checked is not None:
        _, source = checked
        if (any(result.get(field) != source['response']['json'].get(field)
                for field in ('document_id', 'document_sha256', 'spec_hash')) or 'tool_calls' in result):
            _fail('交付恢复不能修改已保存文档指针或调用工具。')


def schedule(h, run_id, role, purpose, hid, result, error):
    from .implementation_alignment_evidence import READ_PENDING
    from .implementation_author_response_correction import KEY as FORMAT_KEY
    from .implementation_save_resume import _settled_response
    from .execution_archive import save
    engine = h.engine
    progress = copy.deepcopy(engine.store.get('handoff_progress', hid, {}))
    if not str(error).startswith(READ_PENDING) or not progress.get(reading.KEY):
        return False
    if progress.get(KEY) is not None:
        _fail('唯一无工具收尾后仍缺必要证据；原答复与草稿已保留。')
    if progress.get(FORMAT_KEY) is not None or role != 'engineer' or not purpose.startswith('implementation_spec:'):
        return False
    engine.check_active(run_id)
    scope, record, context = _scope(h, run_id, role, purpose, hid, progress)
    grant = progress[reading.KEY]
    source = progress.get('finalization' if progress.get('mode') == 'final_only' else 'normal_request', {})
    if (source.get('status') != 'response_saved' or not source.get('operation_id')
            or source.get('withheld_tool_calls') or source.get('response', {}).get('json') != result
            or source.get('prepared', {}).get('role') != role
            or source.get('prepared', {}).get('context', {}).get('_purpose') != purpose
            or not _settled_response(engine.store, run_id, source)
            or source.get('provider_id') is None and engine.store.get('runs', run_id, {}).get('mode') != 'demo'):
        _fail('只允许已完整接收、结算且没有待执行工具的原答复。')
    if hasattr(h.intelligence, 'provider_for'):
        provider = h.intelligence.provider_for(role)
        if (source.get('provider_id'), source.get('model')) != (provider.get('id'), provider.get('model')):
            _fail('原请求供应方或模型已改变。')
    missing = reading._missing(engine, run_id, hid, result, record, context)
    if not missing:
        return False
    refs = [ref for reply in result['issue_responses'] for ref in reply['evidence']
            if ref['document_id'] == grant['document']['document_id']]
    _subset(refs, grant['missing_ranges'])
    _retrieved(engine, run_id, progress, grant['document'], grant['missing_ranges'])
    # A retrieval-only gap is not enough: the original prepared window must
    # actually have replaced at least one missing passage with a reference.
    if not _clipped(source, progress, grant['document'], missing):
        _fail('原请求没有可核验的宿主窗口裁剪，不能增加收尾请求。')
    operation = uid()
    inputs = engine.store.get('handoff_inputs', hid)
    control = {'mode': 'final_only', 'operation_id': operation,
        'reason': 'Once deliver already retrieved required draft ranges; no new tools'}
    prepared = h._document_prepare(run_id, role, purpose, inputs['instruction'], context,
        author.submission_schema(context), progress['tool_results'], final_control=control)
    prepared = h._working_request(run_id, role, hid, progress, prepared)
    projection = prepared.get('context', {}).get('document_read_window', {}).get('mandatory_read_projection', {})
    if (projection.get('document') != grant['document'] or projection.get('unread_required_chars') != 0
            or projection.get('protected_read_chars') != sum(row['limit'] for row in grant['missing_ranges'])):
        _fail('新请求未完整保留原必读范围，未领取收尾。')
    prepared['task'] += ('\n宿主已修复必要原文的窗口裁剪：本次使用原工具回执中已取回的同版范围。'
        '禁止调用工具、重新写稿或扩大来源；核对实际交付原文后，返回同一保存稿的完整提交与逐项答复。'
        '如属于修改，应列出真实变化字段；不得声称科学通过或代替三方独立审核。')
    from .implementation_phase_context import assert_context
    assert_context(prepared['context'])
    archive = save(engine.store, run_id, role, hid + ':author-delivery-recovery:v1',
        {'request': source}, handoff_id=hid, request_id=source['response'].get('request_id'))
    if archive.get('redacted'):
        _fail('原答复含已清理敏感信息，不能完整绑定恢复。')
    final = {'status': 'ready', 'operation_id': operation, 'prepared': prepared, 'reason': control['reason'],
        'created_at': now(), 'withheld_tool_calls': [], 'author_delivery_of': source['operation_id'],
        'completed_result_hashes': [digest(row) for row in progress.get('tool_results', [])]}
    from .tool_request_recovery import prepared_successor
    final = prepared_successor(engine.store, hid, final)
    audit = {'version': 1, 'scope_version': SCOPE_VERSION, 'run_id': run_id, 'role': role, 'purpose': purpose,
        'handoff_id': hid, 'attempt': 1, 'scope': scope, 'source_archive_id': archive['id'],
        'source_archive_sha256': archive['sha256'], 'source_request_sha256': digest(source),
        'source_operation_id': source['operation_id'], 'candidate_sha256': digest(result),
        'operation_id': operation, 'prepared_sha256': digest(prepared), 'created_at': now(),
        'missing_delivery_ranges': copy.deepcopy(missing), 'tools_allowed': False,
        'scientific_approval': False, 'old_request_replayed': False}
    audit['sha256'] = digest(audit)
    def claim(current):
        if current != progress or _scope(h, run_id, role, purpose, hid, current)[0] != scope:
            _fail('原检查点改变或交付恢复已领取，未重复派发。')
        return {**current, KEY: audit, 'mode': 'final_only', 'finalization': final}
    engine.check_active(run_id)
    engine.store.mutate('handoff_progress', hid, claim)
    engine.store.event(run_id, 'implementation.author_delivery_recovery',
        '必要原文已取回但被宿主窗口裁剪；仅一次无工具收尾，保留原付费答复和三方审核门禁。',
        {'handoff_id': hid, 'operation_id': operation, 'source_operation_id': source['operation_id'],
         'source_archive_id': archive['id'], 'missing_delivery_chars': sum(row['limit'] for row in missing),
         'scientific_approval': False, 'tools_allowed': False})
    return True
