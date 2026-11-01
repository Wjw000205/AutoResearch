"""One canonical copy of frozen sources for narrowly scoped repair decisions.

The full packet and paid review records remain persisted. Model requests need
the exact source, current/original demands and dissent, not duplicate excerpts,
receipt schemas or every field of already authenticated review decisions.
"""
from __future__ import annotations

import copy
import re
import ast

from . import code_repair_contract as repair


def full_text(packet, source):
    parts, cursor = [], 0
    for segment in source['segments']:
        if segment['offset'] != cursor or segment['limit'] <= 0:
            repair.fail('完整审查来源不连续。')
        text = segment.get('text')
        if text is None:
            ref = segment['provided_text']
            value = {'repair_packet': packet}
            for key in ref['json_pointer'].split('/')[1:]:
                value = value[int(key)] if isinstance(value, list) else value[key]
            text = value[ref['offset']:ref['offset'] + ref['limit']]
        if not isinstance(text, str) or len(text) != segment['limit']:
            repair.fail('完整审查来源范围发生变化。')
        parts.append(text)
        cursor += len(text)
    if cursor != source['total_chars']:
        repair.fail('完整审查来源正文缺失。')
    return ''.join(parts)


def complete_view(packet):
    sources = packet.get('audit_sources')
    if not sources:
        # Older authenticated packets may not have complete source bundles.
        return copy.deepcopy(packet)
    result = {k: copy.deepcopy(packet[k]) for k in
        ('version', 'source_sha256', 'spec_hash', 'delivery_hash', 'review_scope')}
    result['view_version'] = 2
    result['source_packet_sha256'] = repair.sha(packet)
    if packet.get('audit_revision'):
        result['previous_audit'] = copy.deepcopy(packet['audit_revision'])
    result['documents'] = [{**{k: copy.deepcopy(v) for k, v in doc.items() if k != 'segments'},
        'text': full_text(packet, doc)} for doc in sources['documents']]
    result['code_files'] = {path: {**{k: copy.deepcopy(v) for k, v in doc.items() if k != 'segments'},
        'text': full_text(packet, doc)} for path, doc in sources['code_files'].items()}
    result['acceptance_checks'] = {}
    for group in ('items', 'deferred_items'):
        result[group] = []
        for item in packet.get(group, []):
            result['acceptance_checks'][item['requirement_id']] = copy.deepcopy(item['acceptance_check'])
            row = {k: copy.deepcopy(item[k]) for k in ('issue_id', 'role', 'issue', 'requirement_id',
                'fixes', 'reason', 'disposition', 'allowed_paths', 'amendment') if k in item}
            row['host_evidence'] = [{k: copy.deepcopy(v) for k, v in evidence.items()
                if k not in {'code_text', 'implementation_text'}} for evidence in item['host_evidence']]
            result[group].append(row)
    result['clarifications'] = [{'role': vote['role'], 'decision_sha256': repair.sha(vote),
        'approve': vote['approve'], 'responses': copy.deepcopy(vote.get('clarification_responses', []))}
        for vote in packet.get('clarification', {}).get('votes', [])]
    result['policy'] = ('documents/code_files 提供同一冻结来源的完整正文，各只出现一次。'
        'host_evidence.code_reference 保留原字符范围和实现文档引用。'
        'review_scope 是原问题标准，items 是当前拟修改要求；澄清与修订不代表代码通过。'
        'deferred_items 仍未解决，不授权修改；不得忽略其与当前修改的实际冲突。')
    return result


def excerpt(text, offset, limit):
    end = min(len(text), offset + limit)
    return {'offset': offset, 'limit': end - offset, 'start_line': text.count('\n', 0, offset) + 1,
            'end_line': text.count('\n', 0, end) + 1, 'text': text[offset:end]}


def view(packet, target_ids=None):
    """Issue -> approved clause -> actual code location -> proposed fix.

    Small exact excerpts are navigation evidence, never a claim that an entire
    document/file was read. Missing ranges can be requested explicitly.
    """
    full = complete_view(packet)
    if 'documents' not in full:
        return full
    selected = [i for i in packet['items'] if target_ids is None or i['issue_id'] in target_ids]
    ids = {i['issue_id'] for i in selected}
    result = {k: copy.deepcopy(full[k]) for k in ('version', 'view_version', 'source_packet_sha256',
        'source_sha256', 'spec_hash', 'delivery_hash')}
    if 'previous_audit' in full:
        result['previous_audit'] = full['previous_audit']
    result['items'] = [i for i in full['items'] if i['issue_id'] in ids]
    result['deferred_items'] = [{k: i[k] for k in ('issue_id', 'role', 'issue', 'requirement_id', 'fixes')}
        for i in full['deferred_items']]
    for item in result['items']:
        item.pop('host_evidence', None)
        item['implementation_locations'] = [p for original in selected if original['issue_id'] == item['issue_id']
            for e in original['host_evidence'] for p in e['code_reference']['implementation_refs']]
        item['implementation_locations'] = list({repair.sha(p): p for p in item['implementation_locations']}.values())
    result['original_standards'] = [i for i in (full.get('review_scope') or {}).get('issues', []) if i['issue_id'] in ids]
    result['documents'], result['code_files'] = [], {}
    words = ' '.join(i['issue'] + ' ' + str(i['fixes']) for i in selected)
    for doc in full['documents']:
        body = doc['text']
        parts = []
        # Exact approved acceptance fields give the document location without
        # injecting the whole implementation document or its scientific history.
        for item in selected:
            check = full['acceptance_checks'][item['requirement_id']]
            for key in ('requirement', 'verification'):
                value = check.get(key, '')
                paragraphs = [p for p in value.splitlines() if p.strip()]
                mentioned = set(re.findall(r'\bT(\d{2})\b|test_(\d{2})', item['issue'] + str(item['fixes'])))
                tests = {a or b for a, b in mentioned}
                for paragraph in paragraphs:
                    named = re.match(r'\s*T(\d{2})\b', paragraph)
                    if named and tests and named[1] not in tests:
                        continue
                    offset = body.find(paragraph)
                    if offset >= 0:
                        parts.append(excerpt(body, offset, min(len(paragraph), 1800)))
        # Explicitly cited supplement clauses remain available as exact ranges.
        refs = {'s' + n for n in re.findall(r'(?<![A-Za-z0-9_])s(\d+)(?!\d)', words)}
        for section in doc['sections']:
            if section['id'] in refs:
                parts.append(excerpt(body, section['start'], min(section['end'] - section['start'], 1800)))
        if not parts:
            for item in selected:
                for evidence in item['host_evidence']:
                    for old in evidence['implementation_text']:
                        pointer = old['reference']
                        if pointer['document_id'] == doc['document_id']:
                            section = next(s for s in doc['sections'] if s['id'] == pointer['section_id'])
                            parts.append(excerpt(body, section['start'] + pointer['offset'], min(pointer['limit'], 1200)))
        parts = list({(p['offset'], p['limit']): p for p in parts}.values())
        for part in parts:
            part['section_id'] = next(s['id'] for s in doc['sections'] if s['start'] <= part['offset'] < s['end'])
        supplied = {p['section_id'] for p in parts}
        result['documents'].append({k: copy.deepcopy(doc[k]) for k in ('document_id', 'sha256', 'total_chars')} |
            {'sections': [s for s in doc['sections'] if s['id'] in supplied],
             'section_ranges': {s['id']: [s['start'], s['end']] for s in doc['sections']}, 'excerpts': parts})
    for item in selected:
        locations = ' '.join(f['location'] for f in item['fixes'])
        paths = set(item.get('named_delivery_paths', [])) | {e['path'] for e in item['host_evidence']}
        for path in paths:
            if path not in full['code_files']:
                continue
            source = full['code_files'][path]
            text, spans = source['text'], []
            try:
                tree = ast.parse(text) if path.endswith('.py') else None
            except SyntaxError:
                tree = None
            if tree:
                lines = text.splitlines(keepends=True)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and re.search(
                            r'(?<![A-Za-z0-9_])' + re.escape(node.name) + r'(?![A-Za-z0-9_])', locations):
                        start = sum(map(len, lines[:node.lineno - 1]))
                        size = sum(map(len, lines[node.lineno - 1:node.end_lineno]))
                        spans.append(excerpt(text, start, min(size, 1800)) | {'symbol': node.name})
            if not spans:
                refs = [e['code_reference'] for e in item['host_evidence'] if e['path'] == path]
                spans = [excerpt(text, ref['offset'], min(ref['limit'], 1200)) for ref in refs[:1]]
            result['code_files'].setdefault(path, {k: source[k] for k in ('artifact_id', 'sha256', 'total_chars')} |
                                           {'excerpts': []})['excerpts'].extend(spans)
    for source in result['code_files'].values():
        source['excerpts'] = list({(p['offset'], p['limit']): p for p in source['excerpts']}.values())
    result['policy'] = ('按原审查意见、实现文档定位、实际代码定位、修改和验证要求核对。'
        'excerpts 仅为所标字符范围的真实原文；其他目录和定位不是已读证据。'
        '不带整份文档、源码或评审历史。缺少决定性依据时申请具体位置，不推测，不将未决事项视为已解决。')
    return result


def rebase_sources(packet, old):
    """Moving an unconfirmed issue to deferred must keep every excerpt exact."""
    if not packet.get('audit_sources'):
        return
    positions = {item['issue_id']: f'/{group}/{i}' for group in ('items', 'deferred_items')
                 for i, item in enumerate(packet.get(group, []))}
    replacements = {f'/repair_packet/{group}/{i}/': '/repair_packet' + positions[item['issue_id']] + '/'
        for group in ('items', 'deferred_items') for i, item in enumerate(old.get(group, []))}
    for source in [*packet['audit_sources']['documents'], *packet['audit_sources']['code_files'].values()]:
        for segment in source['segments']:
            if 'provided_text' in segment:
                pointer = segment['provided_text']['json_pointer']
                prefix = next((key for key in replacements if pointer.startswith(key)), None)
                if prefix is None:
                    repair.fail('修改意见调整后无法追溯原文引文。')
                segment['provided_text']['json_pointer'] = replacements[prefix] + pointer[len(prefix):]
