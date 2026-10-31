"""Forget processed author bodies at a real save boundary, not durable evidence."""
from __future__ import annotations

import copy
import hashlib

from .implementation_review_routing import digest

GUIDANCE = ('\nprocessed_archive 表示最近保存之前已处理的材料，正文只保存在原交接账本和不可变文档中，'
    '不再自动塞入每次请求。归档索引不是本次已载入正文，也不产生阅读或批准记录。'
    '保留的最近保存、失败位置和保存后工具结果是当前工作集；不要重新通读归档。'
    '先依据修改意见、最新保存版本、工作笔记和失败回执继续；确需细节时才按目录与 SHA 定向补读。'
    '当前稿未载入字段由编辑工具原样保留，禁止猜测或用旧版覆盖。')


def _page_text(page, parts):
    """Reconstruct an evicted fresh page only against its exact text hash.

    The read window may expose just fragments. Validated revision memory still
    holds the original page; losing it here made consecutive pages evict each
    other and trigger repeated reads. No archived/pre-checkpoint page is passed
    to this helper.
    """
    if isinstance(page.get('text'), str):
        return page['text']
    ref = page.get('document_read_reference', {})
    start, end = page.get('char_start'), page.get('char_end')
    if (type(start) is not int or type(end) is not int or not 0 < end-start <= 6000
            or ref.get('text_chars') != end-start):
        return None
    cursor, pieces = start, []
    for part in sorted(parts, key=lambda p: p['char_start']):
        if (not all(part.get(k) == page.get(k) for k in ('document_id', 'sha256', 'section_id'))
                or not part['char_start'] <= cursor < part['char_end']):
            continue
        right = min(end, part['char_end'])
        pieces.append(part['text'][cursor-part['char_start']:right-part['char_start']])
        cursor = right
        if cursor == end:
            text = ''.join(pieces)
            return text if hashlib.sha256(text.encode()).hexdigest() == ref.get('text_sha256') else None
    return None


def project(context):
    """Called only on a copied new request; prepared requests remain unchanged.

    A successful (possibly partial) save is the explicit processing checkpoint.
    Keep that entire save and every later result, including a failed retry.
    The saved version and cumulative revision progress retain unresolved units.
    """
    memory = context.get('implementation_revision_memory', {})
    if memory.get('version') != 3 or memory.get('processed_archive'):
        return
    from .implementation_revision_memory import validate
    validate(context)
    rows = context.get('tool_results', [])
    fresh = memory['freshness']
    matches = [i for i, row in enumerate(rows) if isinstance(row, dict)
        and row.get('call', {}).get('name') == 'implementation.edit_document'
        and isinstance(row.get('result'), dict) and row['result'].get('saved') is True
        and row['result'].get('submission', {}).get('document_id') == fresh['document_id']]
    continuation = context.get('implementation_acceptance_author_continuation', {})
    checkpoint = continuation.get('document', {})
    # A successor starts at the authenticated saved draft too. Its own ledger
    # has no save yet; that must not resurrect the predecessor's processed body.
    inherited_checkpoint = (not matches and fresh.get('saved_versions') == 0
        and continuation.get('source_handoff_id')
        and checkpoint.get('document_id') == fresh['document_id']
        and checkpoint.get('sha256') == fresh['document_sha256']
        and continuation.get('spec_hash') == fresh['spec_hash'])
    if not matches and not inherited_checkpoint:
        return
    boundary = max(matches) if matches else 0
    archived = []
    for i, row in enumerate(rows[:boundary]):
        call, result = row.get('call', {}), row.get('result', {})
        prior = row.get('archived_call', {})
        item = {'receipt_index': i, 'name': call.get('name', prior.get('name')),
                'receipt_sha256': prior.get('receipt_sha256', digest(row))}
        if isinstance(result, dict):
            for key in ('document_id', 'sha256', 'section_id', 'char_start', 'char_end', 'saved'):
                if key in result:
                    item[key] = copy.deepcopy(result[key])
            if 'submission' in result:
                item['submission'] = copy.deepcopy(result['submission'])
            item['failed'] = bool(result.get('error') or result.get('saved') is False)
        archived.append(item)
    context['tool_results'] = rows[boundary:]
    # Old and current full fields are retrievable by the exact immutable version.
    # Retain hashes and directories, not a second copy of the document.
    previous, current = memory['previous_output'], memory['current_output']
    for output in (previous, current):
        fields = output.get('fields', {})
        output['field_sha256'] = {**output.get('field_sha256', {}),
                                 **{key: digest(value) for key, value in fields.items()}}
        output['omitted_fields'] = sorted(set(output.get('omitted_fields', []))
            | set(fields) | set(output.get('inherited_fields', [])))
        output['fields'] = {}
    current['inherited_fields'] = []
    current['field_sections'] = {}
    checkpoint_context = context.get('acceptance_author_checkpoint')
    if (isinstance(checkpoint_context, dict)
            and checkpoint_context.get('document', {}).get('document_id') == fresh['document_id']
            and checkpoint_context.get('spec_hash') == fresh['spec_hash']):
        checkpoint_context['fields'] = {}
    # Keep only exact newly requested pages after the save. This lets the read
    # planner and duplicate-read guard reuse fresh text without resurrecting
    # the old 64k history window. The compiler deduplicates identical strings.
    pages = [r['result'] for r in rows[boundary:]
             if r.get('call', {}).get('name') == 'documents.read'
             and isinstance(r.get('result'), dict) and not r['result'].get('error')]
    pages = [(page, _page_text(page, memory.get('restored_reading', []))) for page in pages]
    memory['restored_reading'] = [part for part in memory.get('restored_reading', [])
        if any(all(part.get(k) == page.get(k) for k in ('document_id', 'sha256', 'section_id'))
            and type(page.get('char_start')) is int and type(page.get('char_end')) is int
            and page['char_start'] <= part['char_start'] < part['char_end'] <= page['char_end']
            and text is not None
            and text[part['char_start']-page['char_start']:part['char_end']-page['char_start']] == part['text']
            for page, text in pages)]
    memory['processed_archive'] = {'version': 1, 'handoff_id': fresh['handoff_id'],
        'collection': 'handoff_progress', 'checkpoint_document_id': fresh['document_id'],
        'checkpoint_document_sha256': fresh['document_sha256'],
        'receipt_count': len(archived), 'ledger_range': [0, boundary],
        'receipts_sha256': digest(archived), 'receipts': archived[-8:],
        'reading_credit': False, 'scientific_approval': False}
    if inherited_checkpoint:
        memory['processed_archive']['source_handoff_id'] = continuation['source_handoff_id']
        memory['processed_archive']['continuation_sha256'] = continuation['sha256']
    if memory.get('revision_worklist'):
        from .implementation_revision_worklist import complete
        memory['revision_worklist'] = complete(memory['revision_worklist'], memory)
    memory['sha256'] = digest({k: v for k, v in memory.items() if k != 'sha256'})
    context['document_policy'] = GUIDANCE
    validate(context)
