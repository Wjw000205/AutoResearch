"""Complete frozen audit sources without duplicating already supplied excerpts.

This is host-read evidence for semantic review, never an author reading receipt
or code-edit permission. Source paths may join a proposed scope only when named
by that original issue and present in the same immutable delivery.
"""
from __future__ import annotations

import copy
import re

from . import code_repair_contract as repair


def complete_segments(text, ranges):
    """Cover the entire source with exact gaps and pointers to visible excerpts."""
    spans, cursor = [], 0
    for start, end, pointer, excerpt in sorted(ranges):
        if not 0 <= start < end <= len(text) or text[start:end] != excerpt:
            repair.fail('独立核对原文与已选证据范围不一致。')
        if start > cursor:
            spans.append({'offset': cursor, 'limit': start - cursor, 'text': text[cursor:start]})
            cursor = start
        if end > cursor:
            spans.append({'offset': cursor, 'limit': end - cursor,
                'provided_text': {'json_pointer': pointer, 'offset': cursor - start, 'limit': end - cursor}})
            cursor = end
    if cursor < len(text):
        spans.append({'offset': cursor, 'limit': len(text) - cursor, 'text': text[cursor:]})
    return spans


def enrich(engine, run_id, source, packet):
    from .document_index import load
    from .implementation_alignment_evidence import _code_text
    documents, code = {}, {}
    context = source['source_contexts']['director']
    approved = source['implementation_document']
    identities = [(approved['document_id'], approved['sha256'])]
    # The adopted scientific definitions carry inherited formulas, failure
    # semantics and precedence. A locator alone does not provide their text.
    identities += [(d['document_id'], d['document_sha256'])
        for d in context.get('implementation_alignment_contract', {}).get('primary_sources', [])
        if d.get('source_role') == 'approved_definition_document']
    for doc_id, sha in dict.fromkeys(identities):
        manifest, body = load(engine, run_id, doc_id, sha)
        documents[(doc_id, sha)] = {'document_id': doc_id, 'sha256': sha,
            'sections': copy.deepcopy(manifest['sections']), 'body': body, 'ranges': []}
    for group in ('items', 'deferred_items'):
        for number, item in enumerate(packet.get(group, [])):
            scoped = source['source_contexts'][item['role']]
            available = {row['path']: row for row in scoped['delivery_artifacts']}
            locations = ';'.join(f['location'] for f in item['fixes']).replace('\\', '/')
            named = [path for path in available if re.search(
                r'(?<![A-Za-z0-9_./-])' + re.escape(path) + r'(?![A-Za-z0-9_./-])', locations)]
            item['named_delivery_paths'] = sorted(named)
            item['allowed_paths'] = sorted(set(item['allowed_paths']) | set(named))
            paths = set(named) | {row['path'] for row in item['host_evidence']}
            for path in paths:
                bound = available[path]
                if path in code:
                    if code[path]['sha256'] != bound['sha256']:
                        repair.fail('独立核对的角色代码版本不一致。')
                    continue
                code[path] = {'artifact_id': bound['artifact_id'], 'sha256': bound['sha256'],
                    'body': _code_text(engine, run_id, bound['artifact_id'], bound), 'ranges': []}
            for index, evidence in enumerate(item['host_evidence']):
                pointer = f'/repair_packet/{group}/{number}/host_evidence/{index}'
                ref = evidence['code_reference']
                code[evidence['path']]['ranges'].append((ref['offset'], ref['offset'] + ref['limit'],
                    pointer + '/code_text', evidence['code_text']))
                for j, row in enumerate(evidence['implementation_text']):
                    ref = row['reference']
                    doc = documents[(ref['document_id'], ref['document_sha256'])]
                    section = next(s for s in doc['sections'] if s['id'] == ref['section_id'])
                    start = section['start'] + ref['offset']
                    doc['ranges'].append((start, start + ref['limit'],
                        pointer + f'/implementation_text/{j}/text', row['text']))
    def finish(value):
        body, ranges = value.pop('body'), value.pop('ranges')
        return {**value, 'total_chars': len(body), 'segments': complete_segments(body, ranges)}
    packet['audit_sources'] = {'version': 1,
        'documents': [finish(value) for value in documents.values()],
        'code_files': {path: finish(value) for path, value in sorted(code.items())},
        'policy': '完整宿主核验原文。segments 按字符 offset/limit 连续覆盖全文；text 为原文，'
            'provided_text 按 json_pointer 取本包已展示原文，再取其相对 offset/limit，不能当目录略过。'
            '只消除重复正文，无摘要或删节；批准规范、继承来源、未决事项和代码均保留真实版本。'
            'named_delivery_paths 仅来自原问题 fixes.location 且存在于同一冻结交付；'
            '补齐候选修改范围仍须两位独立评审核对相关性和规范依据，不是派发授权。'}
    return packet
