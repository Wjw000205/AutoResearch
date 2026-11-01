"""Pure exact-source coverage from real same-handoff document tool receipts."""
from __future__ import annotations

READ_PENDING = '对齐依据尚未读完，请继续同版本核对'


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked(message)


def coverage(engine, run_id, document, required, handoff_ids, *, include_memory=True):
    """Return real covered/missing ranges; corrupt receipts are not missing reads."""
    from . import document_index
    manifest, text = document_index.load(engine, run_id, document['document_id'], document['sha256'])
    sections = {section['id']: section for section in manifest['sections']}
    spans = {}
    for hid in handoff_ids:
        from .evidence_delivery import tracking_required, delivery_spans
        tracked = tracking_required(engine.store, run_id, hid)
        delivered = []
        if tracked:
            role = engine.store.get('handoffs', hid, {}).get('to_role')
            delivered = delivery_spans(engine.store, run_id, role, document['document_id'],
                document['sha256'], identity_key='document_id', handoff_id=hid, text=text)
        progress = engine.store.get('handoff_progress', hid, {})
        if progress and (progress.get('id') != hid or progress.get('run_id') != run_id):
            _fail('实现审查实际读取账本不属于当前交接。')
        for receipt in progress.get('tool_results', []):
            if not isinstance(receipt, dict):
                _fail('实现审查实际读取回执已损坏，不能以补读掩盖。')
            if receipt.get('predecessor_handoff_id') is not None:
                continue
            call, page = receipt.get('call', {}), receipt.get('result', {})
            if include_memory and isinstance(call, dict) and call.get('name') == 'documents.recall' and isinstance(page, dict) and not page.get('error'):
                from .code_navigation import verify_recall
                role = engine.store.get('handoffs', hid, {}).get('to_role')
                verify_recall(engine, run_id, role, hid, call.get('arguments', {}), page)
                for entry in page.get('entries', []):
                    if entry.get('document_id') == document['document_id'] and entry.get('sha256') == document['sha256']:
                        spans.setdefault(entry['section_id'], []).append((entry['char_start'], entry['char_end']))
                continue
            if not isinstance(call, dict) or call.get('name') != 'documents.read':
                continue
            args = call.get('arguments', {})
            if not isinstance(args, dict):
                _fail('实现审查实际读取参数已损坏，不能以补读掩盖。')
            if args.get('document_id') != document['document_id']:
                continue
            if isinstance(page, dict) and page.get('error'):
                continue
            if not isinstance(page, dict):
                _fail('实现审查实际读取正文回执已损坏，不能以补读掩盖。')
            section = sections.get(args.get('section_id'))
            start, end = page.get('char_start'), page.get('char_end')
            offset, limit = args.get('offset', 0), args.get('limit', 4000)
            if (section is None or type(start) is not int or type(end) is not int
                    or type(offset) is not int or type(limit) is not int or not 1 <= limit <= 6000
                    or not section['start'] <= start <= end <= section['end']
                    or start != section['start'] + offset or end > start + limit
                    or args.get('expected_sha256') != document['sha256']
                    or page.get('document_id') != document['document_id']
                    or page.get('sha256') != document['sha256']
                    or page.get('section_id') != section['id'] or page.get('text') != text[start:end]):
                _fail('实现审查实际读取回执的版本、范围或正文不一致，不能以补读掩盖。')
            if tracked:
                # A completed read is retrieval. Only its actually submitted
                # fragments contribute delivery credit after prompt compression.
                for row in delivered:
                    if row.get('section_id') == section['id']:
                        a, b = max(start, row['char_start']), min(end, row['char_end'])
                        if a < b:
                            spans.setdefault(section['id'], []).append((a, b))
            else:
                spans.setdefault(section['id'], []).append((start, end))
    covered, missing = [], []
    for receipt in required:
        page = receipt['result']
        start, end, section_id = page['char_start'], page['char_end'], page['section_id']
        cursor = start
        for a, b in sorted(spans.get(section_id, [])):
            a, b = max(start, a), min(end, b)
            if b <= cursor or b <= a:
                continue
            if a > cursor:
                missing.append({'section_id': section_id, 'start': cursor, 'end': a})
                cursor = a
            covered.append({'section_id': section_id, 'start': cursor, 'end': b})
            cursor = b
        if cursor < end:
            missing.append({'section_id': section_id, 'start': cursor, 'end': end})
    return {'covered_ranges': covered, 'missing_ranges': missing,
            'covered_chars': sum(item['end'] - item['start'] for item in covered),
            'complete': not missing}
