"""Acceptance read navigation and authenticated, actionable coverage failures."""
from __future__ import annotations

from . import implementation_acceptance as policy

PLAN = 'implementation_acceptance_read_plan'


def required(sections):
    return [{'result': {'section_id': s['id'], 'char_start': s['start'], 'char_end': s['end']}}
            for s in sections]


def gaps(engine, run_id, row, packet, criterion):
    from .implementation_reading import coverage
    scope = packet['context'][policy.CONTEXT]
    frozen = scope['required_sections'][criterion]
    mapped = policy.required_sections(row, scope['contract'], [criterion])
    original = coverage(engine, run_id, row['document'], required(frozen), [packet['handoff_id']])
    current = coverage(engine, run_id, row['document'], required(mapped), [packet['handoff_id']])
    return {'criterion_id': criterion, 'frozen_missing_ranges': original['missing_ranges'],
            'mapped_missing_ranges': current['missing_ranges'],
            'frozen_sections': [s['id'] for s in frozen], 'mapped_sections': [s['id'] for s in mapped]}


def reject(packet, missing):
    from .engine import ResearchBlocked
    scope = packet['context'][policy.CONTEXT]
    sections = {s['id']: s for s in scope['document']['sections']}
    names = list(dict.fromkeys(sections[r['section_id']]['title']
        for item in missing for r in item['mapped_missing_ranges']))
    criteria = '、'.join(item['criterion_id'] for item in missing)
    detail = '；当前依赖仍缺：' + '、'.join(names) if names else '；原冻结目录映射已过时，需原角色重新核验有效范围'
    error = ResearchBlocked('实现文档验收：' + criteria + ' 的 PASS 缺少实际读取与投递覆盖' + detail + '。')
    error.acceptance_read_coverage_error = {'version': 1, 'kind': 'incomplete_acceptance_reading',
        'document_id': scope['document']['document_id'], 'document_sha256': scope['document']['sha256'],
        'criteria': missing}
    raise error


def plan(context, receipts):
    """Navigation only: retrieval is not a submitted-delivery or approval proof."""
    from .document_read_window import _uncovered
    if context.get(PLAN) != 1 or policy.CONTEXT not in context:
        return None
    scope = context[policy.CONTEXT]; doc = scope['document']
    sections = {s['id']: s for values in scope['required_sections'].values() for s in values}
    spans = {sid: [] for sid in sections}
    for receipt in receipts:
        call, page = receipt.get('call', {}), receipt.get('result', {})
        args = call.get('arguments', {})
        if (call.get('name') != 'documents.read' or not isinstance(page, dict) or page.get('error')
                or args.get('document_id') != doc['document_id'] or args.get('expected_sha256') != doc['sha256']):
            continue
        sid = args.get('section_id'); section = sections.get(sid)
        a, b = page.get('char_start'), page.get('char_end')
        offset, limit = args.get('offset', 0), args.get('limit', 4000)
        if (section is None or type(a) is not int or type(b) is not int
                or type(offset) is not int or type(limit) is not int or not 1 <= limit <= 6000
                or not section['start'] <= a < b <= section['end'] or a != section['start'] + offset
                or b > a + limit or page.get('document_id') != doc['document_id']
                or page.get('sha256') != doc['sha256'] or page.get('section_id') != sid
                or not isinstance(page.get('text'), str) or len(page['text']) != b-a):
            continue
        spans[sid].append((a, b, ''))
    missing, calls = [], []
    for sid, section in sections.items():
        for a, b in _uncovered(section['start'], section['end'], spans[sid]):
            missing.append({'section_id': sid, 'title': section['title'], 'start': a, 'end': b})
            for start in range(a, b, 6000):
                calls.append({'name': 'documents.read', 'arguments': {'document_id': doc['document_id'],
                    'expected_sha256': doc['sha256'], 'section_id': sid, 'offset': start-section['start'],
                    'limit': min(6000, b-start)}})
    return {'version': 1, 'document_id': doc['document_id'], 'document_sha256': doc['sha256'],
        'missing_ranges': missing, 'missing_chars': sum(r['end']-r['start'] for r in missing),
        'next_calls': calls[:3], 'remaining_page_count': len(calls),
        'retrieval_navigation_only': True, 'scientific_approval': False,
        'instruction': '先按 next_calls 补读分配项及依赖的缺失范围。已读取范围保留原文，不重复读；'
            '清单只记录取回进度，最终仍需实际投递和批准来源证据。未核实不能给 PASS。'}
