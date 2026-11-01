"""Bounded author read planning over frozen findings and the latest saved SPEC.

Navigation only: plans neither read a source nor grant coverage or approval.
"""
from __future__ import annotations

import copy

KEY = 'revision_worklist'
MAX_READS = 8
PAGE_CHARS = 6000
GUIDANCE = ('\n批量修订：先同时检查 revision_worklist 中所有 pending_conditions，按 suggested_index_ids '
    '定位当前稿。source_ranges 来自原意见和当前稿显式引用，仅是定位线索，不是完整依赖证明。'
    '先复用记忆，集中列出全部真正缺失的材料；独立的 documents.read 在同一个 tool_calls 中批量请求，'
    '每批最多8处、每处最多6000字符，包括同章节已知长度的后续页。不要每取一页就等待一轮模型。'
    'next_read_batch 是已扣除本次载入正文的候选批次；仅选择本轮修改需要的范围，'
    '不要为清空清单而读取无关材料。没有缺失依赖时立即合并相关索引修改，'
    '一次 edit_document 提交本批所有可完成位置；成功项立即保留，只重试失败项和补齐剩余条件。'
    '全部修改完成后再批量读取当前保存稿进行自检。目录、计划和作者已处理计数都不是独立审核通过。')
GUIDANCE += ('\n续接中的 author_self_check_pending 是上一交接真实自检仍未完成的条件，'
    '即使保存登记为已处理，也必须继续核对，不能只盯着最后一条未保存条件。'
    'blocked_conditions 保留明确的外部缺口；没有新依据时不要反复登记或搜索同一缺口，'
    '先完成其余可做的修改与自检，再如实报告仍被依赖阻塞的条件。')
GUIDANCE += ('\n修订按实现阻塞优先：先补唯一可实现的算法、跨模块接口、数据权限、参数消费和资源边界，'
    '再同步这些改动必需的测试与来源。已有补充定义时，应成组更新算法、参数、来源和相应测试，'
    '不能只修改验收表而让算法继续声称定义缺失。还有可完成的实质修改时，先完成修改再做最终自检，'
    '避免每保存一个关联字段就把全部旧意见重核一遍。未改变行为且已满足原意见的措辞、排版、'
    '重复免责声明不另开修改；保留既定 close_when，不扩大发现范围，也不把外部依赖写成已解决。'
    'write_constraints 给出本次真实字段长度限制。按 suggested_max_chars 留出余量，一次给齐必要定义，'
    '用现有精确引用消除重复说明，不删除公式、参数、分支或停止条件来压缩文本。'
    '采纳有依据的新定义时，直接替换旧定义并移除已被解决的缺失声明，不能只在后面补一段。'
    '长文本优先用 text_replacements 精确替换已定位的段落；配置、来源、测试和资源细节各归所属字段，'
    '算法保留必要定义和准确交叉引用，确保当前稿只有一套一致且可实现的规则。')


def write_constraints(context):
    """Expose effective bounds before generation; never change the save schema."""
    from .implementation_module_details import validation_schema
    schema = context.get('implementation_document_authoring', {}).get('specification_schema', {})
    result = []
    def visit(node, path):
        limit = node.get('maxLength')
        if node.get('type') == 'string' and type(limit) is int:
            result.append({'path': path, 'max_chars': limit,
                           'suggested_max_chars': limit * 9 // 10})
        for key, child in node.get('properties', {}).items():
            visit(child, path + '/' + key)
        if isinstance(node.get('items'), dict):
            visit(node['items'], path + '/*')
    visit(validation_schema(schema, context), '')
    return result


def _subtract(start, end, covered):
    remaining = [(start, end)]
    for left, right in covered:
        remaining = [piece for a, b in remaining for piece in
            ((a, min(b, left)), (max(a, right), b)) if piece[0] < piece[1]]
    return remaining


def _refs(value):
    if isinstance(value, dict):
        if {'document_id', 'document_sha256', 'section_id', 'offset', 'limit'} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


def dependencies(context, current, progress):
    from .implementation_acceptance import criterion_fields
    from .implementation_document_locator import catalog
    from .document_read_index import _merge
    handled = {tuple(pair) for pair in progress.get('handled_conditions', [])}
    self_checks = {(item['issue_id'], item['condition_index']): item
        for item in context.get('acceptance_author_pending', [])
        if item.get('status') in {'incomplete', 'blocked'}}
    journal = context.get('implementation_author_self_check', {})
    resolved = {(c['issue_id'], c['index']) for c in journal.get('valid', [])
                if c['status'] in {'complete', 'blocked'}}
    remaining = {(c['issue_id'], c['index']) for c in journal.get('remaining', [])}
    documents = {d['document_id']: d for d in context.get('indexed_documents', [])}
    entries = catalog(current['spec'])
    pending, ranges, unresolved = [], {}, []
    from .implementation_condition_roles import review_pending
    review = review_pending(context)
    reviewer_conditions = {(r['issue_id'], r['index']) for r in review}
    for issue in context.get('acceptance_issues', []):
        indices = [i for i in range(len(issue['close_when']))
                   if (issue['issue_id'], i) not in reviewer_conditions and (issue['issue_id'], i) not in resolved and (
                       (issue['issue_id'], i) not in handled or (issue['issue_id'], i) in self_checks
                       or (issue['issue_id'], i) in remaining)]
        if not indices:
            continue
        criterion = issue.get('criterion_id')
        fields = criterion_fields(context['acceptance_contract'], criterion) if criterion else set()
        targets = [e['index_id'] for e in entries if e['index_id'] in fields or
                   (e.get('parent_id') in {'architecture_traceability', 'acceptance_checks', 'implementation_map'}
                    and e['index_id'].split('/')[1] == criterion)]
        pending.append({'issue_id': issue['issue_id'], 'condition_indices': indices,
                        'criterion_id': criterion, 'suggested_index_ids': targets})
        values = [issue.get('evidence', []), issue.get('adjudication', {}).get('evidence', []),
                  *[current['spec'].get(field) for field in sorted(fields)]]
        for ref in _refs(values):
            doc = documents.get(ref['document_id'])
            # Prior SPEC citations describe the finding, not new-source reading work.
            if doc and doc.get('kind') in {'implementation_specification', 'document_working_notes'}:
                continue
            section = next((s for s in doc['sections'] if s['id'] == ref['section_id']), None) if doc else None
            offset, limit = ref['offset'], ref['limit']
            if (doc is None or doc['sha256'] != ref['document_sha256'] or section is None
                    or type(offset) is not int or type(limit) is not int or offset < 0 or limit <= 0
                    or offset + limit > section['end'] - section['start']):
                unresolved.append({'issue_id': issue['issue_id'], 'reference': copy.deepcopy(ref),
                                   'reason': '引用版本或范围不匹配；须定位核对，不能自动替换来源。'})
                continue
            key = (doc['document_id'], doc['sha256'], section['id'])
            value = ranges.setdefault(key, {'document_id': key[0], 'sha256': key[1], 'section_id': key[2],
                'section_start': section['start'], 'section_end': section['end'],
                'title': section['title'], 'issue_ids': [], 'ranges': []})
            if issue['issue_id'] not in value['issue_ids']:
                value['issue_ids'].append(issue['issue_id'])
            value['ranges'] = _merge([*value['ranges'],
                [section['start'] + offset, section['start'] + offset + limit]])
    needs_edit = {iid for (iid, _), check in self_checks.items()
                  if check.get('status') in {'incomplete', 'blocked'}}
    pending.sort(key=lambda item: (item['issue_id'] not in needs_edit,
        {'C02': 0, 'C03': 1, 'C04': 2, 'C05': 3, 'C06': 4}.get(
        item['criterion_id'], 0), item['issue_id']))
    return {'version': 1, 'document_id': current['document']['document_id'],
        'write_constraints': write_constraints(context),
        'substantive_revision_first': [p['issue_id'] for p in pending if p['issue_id'] in needs_edit],
        'repair_order': '先成组完成影响实现的正文修改，再核对最终版本；不为措辞或重复状态说明另存版本。',
        'spec_hash': current['spec_hash'], 'pending_conditions': pending, 'review_pending': review,
        'author_self_check_pending': [copy.deepcopy(self_checks[(item['issue_id'], index)])
            for item in pending for index in item['condition_indices']
            if (item['issue_id'], index) in self_checks],
        'blocked_conditions': [copy.deepcopy(self_checks[(item['issue_id'], index)])
            for item in pending for index in item['condition_indices']
            if self_checks.get((item['issue_id'], index), {}).get('status') == 'blocked'],
        'failed_index_ids': [entry['index_id'] for entry in progress.get('failed_units', [])],
        'source_ranges': list(ranges.values()), 'unresolved_references': unresolved,
        'reading_credit': False, 'scientific_approval': False}


def complete(plan, memory):
    """Subtract only text present in this request, not historical read indices."""
    plan = copy.deepcopy(plan)
    calls, missing_chars = [], 0
    for source in plan['source_ranges']:
        covered = [(p['char_start'], p['char_end']) for p in memory['restored_reading']
            if (p['document_id'], p['sha256'], p['section_id']) ==
               (source['document_id'], source['sha256'], source['section_id'])]
        missing = [list(span) for a, b in source['ranges'] for span in _subtract(a, b, covered)]
        source['missing_ranges'] = missing
        for start, end in missing:
            missing_chars += end - start
            while start < end and len(calls) < MAX_READS:
                limit = min(PAGE_CHARS, end - start)
                calls.append({'name': 'documents.read', 'arguments': {
                    'document_id': source['document_id'], 'expected_sha256': source['sha256'],
                    'section_id': source['section_id'], 'offset': start - source['section_start'], 'limit': limit}})
                start += limit
    plan.update(next_read_batch=calls, missing_source_chars=missing_chars,
                max_read_calls=MAX_READS, max_batch_chars=MAX_READS * PAGE_CHARS)
    return plan


def branch(context):
    """A read-only alternative; mutations retain their existing batch contract."""
    from .implementation_acceptance_author import enabled
    from .implementation_revision_memory import KEY as MEMORY, validate
    value = context.get(MEMORY, {})
    if (not enabled(context) or context.get('implementation_document_authoring_protocol') != 1
            or value.get('version') != 3 or value.get(KEY, {}).get('version') != 1):
        return None
    validate(context)
    from .engine import obj
    from .tool_contracts import call_schema
    return obj({'tool_calls': {'type': 'array', 'minItems': 1, 'maxItems': MAX_READS,
        'uniqueItems': True, 'items': call_schema(['documents.read'], context)}})
