"""Scope new engineering-definition reviews and repair legacy source citations."""
from __future__ import annotations

import copy
import re

KEY = 'implementation_definition_review_scope'
REVISION = 'definition_revision_comparison'
CITATION = 'definition_source_citation'
READ_BATCH = 8
OPTIONAL_BATCH = 3
TOOLS = frozenset({'documents.list', 'documents.search', 'documents.read', 'documents.recall',
                   'code.locate', 'code.read', 'code.record_notes'})
TASK = ('独立审核当前实现文档中的工程定义补充，核对合同来源、调用者、实际消费者及验收路径。'
    'resolutions 逐项回答 definition_repair.issues；缺口未闭合时给出证据、所在位置和可验证的修改意见。'
    '沿用原授权及科学判据。批准补充定义只允许进入原流程的下一项工程工作。')
GUIDANCE = ('两位审核者独立核对同一当前实现文档和本问题引用的冻结代码范围。'
    'definition_required_code_ranges 列出必读代码的精确位置，其他列出的关联代码按需查阅。'
    '批准架构按需定位；source_refs 使用相关的当前实现文档或批准架构精确范围。'
    '工具回执仅显示当前批次，已投递范围和原记录可按文档、代码位置或笔记 ID 回查。'
    '只审原问题和新增定义，按原判据提交独立结论。')
VERSION_GUIDANCE = ('\n同一路径可能有两个版本。带 base_attempt 的条目来自原差异的基线；'
    '分别使用引用自己的 artifact_id、SHA256 和字符范围核对，基线内容只证明旧版行为。')
CITATION_TASK = ('你是此次原审核意见的作者。沿用 previous_own_result 的完整答复，只向 source_refs 追加一处'
    '来自 source_candidates、与原意见有关的批准架构精确引用。候选原文是你本人已实际收到的同版本片段。'
    '选用候选中的 reference 对象，保留原有引用和其他字段逐字相同。若无相关片段，保留原答复。')


def enabled(context):
    return type(context.get(KEY)) is int and context[KEY] == 1


def outside_document_scope(context, call):
    args = call.get('arguments', {})
    return (enabled(context) and call.get('name') in {'documents.list', 'documents.search', 'documents.recall'}
        and bool(args.get('document_id')) and not any((doc['document_id'], doc['sha256']) ==
            (args.get('document_id'), args.get('expected_sha256')) for doc in context['indexed_documents']))


def _reading_calls(state):
    from .independent_full_review import sha

    calls = {}
    for target in state['missing_read_targets']:
        for call in target['read_with']:
            calls.setdefault(sha(call), call)
    return list(calls.values())


def schedule(state, turn):
    from .independent_full_review import sha
    from .indexed_review_closeout import EXTRA_TOOL_TURNS

    calls = _reading_calls(state)
    batches = (len(calls) + READ_BATCH - 1) // READ_BATCH
    return {**state, 'tool_schedule': {'started_turn': turn, 'deadline_turn': turn + batches + EXTRA_TOOL_TURNS,
        'calls_sha256': sha(calls)}}


def _scheduled(progress):
    from .engine import ResearchBlocked
    from .full_review_closeout import READING

    state = progress.get(READING)
    if not state:
        return None
    policy = state.get('tool_schedule', {})
    first = policy.get('started_turn')
    if type(first) is not int or not 0 <= first <= progress.get('turn', 0) or schedule(state, first) != state:
        raise ResearchBlocked('工程定义复审的定向补读计划与保存范围不一致。')
    return state, _reading_calls(state), progress['turn'] - first


def tool_turn_limit(progress):
    from .indexed_review_closeout import STATE
    scheduled = _scheduled(progress)
    if scheduled is not None:
        return scheduled[0]['tool_schedule']['deadline_turn']
    if STATE in progress:
        return progress[STATE]['deadline_turn']
    # The owning closeout must first compute a plan at an unsent boundary.
    return progress.get('turn', 0) + 1


def await_scheduled_delivery(progress):
    from .engine import ResearchBlocked, digest
    from .full_review_window import requested_indices

    scheduled = _scheduled(progress)
    if scheduled is None:
        return False
    state, calls, elapsed = scheduled
    start = (elapsed - 1) * READ_BATCH
    if elapsed and start < len(calls):
        rows = progress['tool_results']
        expected = calls[start:start + READ_BATCH]
        indices = requested_indices(progress, rows)
        returned = [rows[index] for index in indices]
        if (sorted(digest(row['call']) for row in returned) != sorted(digest(call) for call in expected)
                or any(not isinstance(row.get('result'), dict) or row['result'].get('error')
                       or not isinstance(row['result'].get('text'), str)
                       or len(row['result']['text']) != row['call']['arguments']['limit'] for row in returned)):
            raise ResearchBlocked('工程定义复审的定向页未按计划完整返回，原请求和工具回执已保存。')
    return progress['turn'] < state['tool_schedule']['deadline_turn']


def prepare_scheduled(progress, prepared):
    from .engine import obj

    state, calls, elapsed = _scheduled(progress)
    batch = calls[elapsed * READ_BATCH:(elapsed + 1) * READ_BATCH]
    if batch:
        prepared['context']['definition_required_read_batch'] = batch
        prepared['task'] += ('\n宿主已按本角色的实际投递回执定位必读缺页。返回 definition_required_read_batch '
            '中全部精确调用；读完后再整理结论或查阅按需架构。')
        prepared['schema'] = obj({'tool_calls': {'type': 'array', 'minItems': len(batch), 'maxItems': len(batch),
            'uniqueItems': True, 'items': {'enum': batch}}})
        return prepared
    remaining = max(0, state['tool_schedule']['deadline_turn'] - progress['turn'])
    prepared['task'] += (f'\n定向缺页批次已返回。若确需核对具体引用或查阅架构，剩余 {remaining} 轮工具额度，'
        f'每轮最多 {OPTIONAL_BATCH} 项。请用当前投递和已有笔记提交最终独立意见。')
    return limit_tool_batch(prepared, OPTIONAL_BATCH)


def limit_tool_batch(prepared, maximum):
    for branch in prepared['schema'].get('anyOf', [prepared['schema']]):
        array = branch.get('properties', {}).get('tool_calls')
        if array is not None:
            array['maxItems'] = min(array.get('maxItems', maximum), maximum)
    return prepared


def citation(context):
    from .independent_full_review import READING
    value = context.get(READING, {})
    problems = value.get('problems', [])
    if value.get('recovery_kind') == 'response_correction' and len(problems) == 1:
        if problems[0].get('kind') == CITATION:
            return problems[0]
    return None


def citation_schema(context, schema):
    from .independent_full_review import READING
    original = context[READING]['previous_own_result']
    problem = citation(context)
    result = copy.deepcopy(schema)
    for field, value in original.items():
        if field != 'source_refs':
            result['properties'][field] = {'const': value}
    refs = original['source_refs']
    result['properties']['source_refs'] = {'enum': [refs, *[
        [*refs, candidate['reference']] for candidate in problem['source_candidates']]]}
    return result


def revision_comparison(engine, run_id, state, row):
    """Compare the two reviewed snapshots of this definition, keeping the original votes attributable."""
    from difflib import SequenceMatcher
    from . import implementation_definition_repair as definitions
    from .implementation_acceptance_flow import check_packet
    from .implementation_authoring import validate_submission
    from .engine import digest

    record = state[definitions.FIELD]
    position = record['revision_indices'].index(row['index'])
    if position == 0:
        raise ValueError('缺少同一补定义链中的上一份复审稿。')
    index = record['revision_indices'][position - 1]
    prior = next(r for r in state['specifications'] if r['index'] == index)
    binding = definitions.binding(state)
    base = definitions.base_row(state, record)['spec']['algorithm']
    packets = prior.get('definition_reviews', [])
    if (prior.get('definition_repair_id') != record['id'] or prior.get('approved') is not False
            or not definitions._review_members(packets) or all(p['result']['approve'] for p in packets)):
        raise ValueError('上一复审稿缺少同一补定义链的完整退回意见。')
    versions, tails = [], []
    for version in (prior, row):
        validate_submission(engine, run_id, state, version)
        definitions.validate_candidate(version['spec'], state, binding)
        algorithm = version['spec']['algorithm']
        if digest(version['spec']) != version['spec_hash']:
            raise ValueError('补定义版本与已保存正文不一致。')
        versions.append({'index': version['index'], 'spec_hash': version['spec_hash'],
            'document_id': version['document']['document_id'], 'document_sha256': version['document']['sha256']})
        tails.append(algorithm[len(base):].splitlines(keepends=True))
    reviews = []
    for packet in packets:
        check_packet(engine, run_id, packet)
        result = packet['result']
        if result['spec_hash'] != prior['spec_hash'] or result['document_sha256'] != prior['document']['sha256']:
            raise ValueError('上一轮评审意见引用了其他版本。')
        reviews.append({'role': packet['role'], 'handoff_id': packet['handoff_id'],
            **{k:copy.deepcopy(result[k]) for k in ('approve','rationale','issues','fixes','resolutions')},
            **{k:copy.deepcopy(result[k]) for k in ('host_asset_next_action','asset_interface_proposal_review') if k in result}})
    left, right = tails
    changes = [{'operation': op, 'previous_lines': [a,b], 'current_lines': [c,d],
                'removed': ''.join(left[a:b]), 'added': ''.join(right[c:d])}
               for op,a,b,c,d in SequenceMatcher(None,left,right,autojunk=False).get_opcodes() if op != 'equal']
    return {'version': 1, 'previous': versions[0], 'current': versions[1], 'previous_reviews': reviews,
            'line_basis': 'zero-based half-open ranges within the engineering addition after the approved algorithm',
            'changes': changes}


def attach(context, document, addition):
    from . import host_asset_definition_recovery as asset_recovery

    issues = context['definition_repair']['issues']
    sources = {row['artifact_id']: row for row in context.get('definition_code_sources', [])}
    required = {}
    for issue in issues:
        for ref in issue['references']:
            if 'artifact_id' not in ref:
                continue
            source = sources.get(ref['artifact_id'])
            if not source or source['sha256'] != ref.get('sha256'):
                raise ValueError('Definition review evidence refers to a different code version')
            identity = (ref['artifact_id'], ref['sha256'], ref['offset'], ref['limit'])
            required[identity] = {'id': identity[0], 'expected_sha256': identity[1],
                'offset': identity[2], 'limit': identity[3]}
    required_ids = {ref['id'] for ref in required.values()}
    code = [row for row in sources.values() if row['artifact_id'] in required_ids or re.search(
        r'(?<![\w./-])' + re.escape(row['path']) + r'(?![\w./-])', addition)]
    fields = ('implementation_definition_repair_protocol', 'definition_repair', 'definition_addition',
        'definition_additions', 'definition_addition_instruction', 'spec_hash', 'proposal_hash',
        'document_sha256', 'design_hash', 'research_cycle', 'architecture_document',
        'whole_definition_review_protocol', asset_recovery.KEY)
    result = {key: copy.deepcopy(context[key]) for key in fields if key in context}
    if any('base_attempt' in source for source in code):
        result['definition_code_source_version'] = 2
    result.update({KEY: 1, 'indexed_documents': [document, context['architecture_document']],
        'independent_full_review_documents': [document], 'definition_code_sources': code,
        'definition_required_code_ranges': list(required.values()), 'document_index_protocol': 1,
        'document_input_protocol': 2, 'indexed_document_context': True})
    return result


def project(prepared, rows, indices):
    from .implementation_whole_reading import union_ranges

    context = dict(prepared['context'])
    reads, notes = {}, []
    for row in rows:
        name = row.get('call', {}).get('name')
        page = row.get('result')
        if not isinstance(page, dict) or page.get('error'):
            continue
        if name == 'code.record_notes':
            notes.extend(page.get('observation_ids', ()))
        if name in {'documents.read', 'code.read'} and isinstance(page.get('text'), str):
            identity = (page.get('document_id') or page['artifact_id'], page['sha256'])
            start = page['char_start'] if name == 'documents.read' else page['offset']
            reads.setdefault(identity, []).append({'section_id': page.get('section_id', 'code'),
                'start': start, 'end': start + len(page['text'])})
    context['definition_review_progress'] = {'retrieved_ranges': [
        {'source_id': sid, 'sha256': sha, 'ranges': union_ranges(spans)} for (sid, sha), spans in reads.items()],
        'recent_note_ids': list(dict.fromkeys(notes))[-12:], 'reading_credit': False}
    selected = [rows[i] for i in indices[-READ_BATCH:]]
    context['tool_results'] = copy.deepcopy(selected)
    return {**prepared, 'context': context}


def validate_reading(engine, run_id, role, context, owners):
    from .evidence_delivery import delivery_spans
    from .implementation_alignment_evidence import _code_text, _own_code_spans
    from .implementation_reading import coverage
    from .implementation_whole_reading import required
    from .review_material_reuse import union

    doc, = context['independent_full_review_documents']
    proof = coverage(engine, run_id, doc, required(doc), owners)
    count, missing = proof['covered_chars'], []
    if not proof['complete']:
        starts = {s['id']: s['start'] for s in doc['sections']}
        calls = [{'name': 'documents.read', 'arguments': {'document_id': doc['document_id'],
            'expected_sha256': doc['sha256'], 'section_id': span['section_id'],
            'offset': offset - starts[span['section_id']], 'limit': min(6000, span['end'] - offset)}}
            for span in proof['missing_ranges'] for offset in range(span['start'], span['end'], 6000)]
        missing.append({'kind': 'document', 'document': doc, 'read_with': calls})

    sources = {row['artifact_id']: row for row in context['definition_code_sources']}
    spans_by_source = {}
    for ref in context['definition_required_code_ranges']:
        aid, sha = ref['id'], ref['expected_sha256']
        if aid not in spans_by_source:
            text = _code_text(engine, run_id, aid, sources[aid])
            spans = []
            for owned in owners:
                read = _own_code_spans(engine, owned, aid, sha, text)
                delivered = delivery_spans(engine.store, run_id, role, aid, sha, text=text,
                    identity_key='artifact_id', handoff_id=owned)
                spans.extend((max(a, r['char_start']), min(b, r['char_end'])) for a, b in read for r in delivered
                    if max(a, r['char_start']) < min(b, r['char_end']))
            spans_by_source[aid] = (len(text), union(spans))
        length, spans = spans_by_source[aid]
        start, end = ref['offset'], ref['offset'] + ref['limit']
        if not 0 <= start < end <= length:
            raise ValueError('Definition review has an invalid frozen code range')
        cursor, gaps = start, []
        for left, right in spans:
            a, b = max(start, left), min(end, right)
            if a >= b:
                continue
            if a > cursor:
                gaps.append((cursor, a))
            cursor = b
            count += b - a
        if cursor < end:
            gaps.append((cursor, end))
        if gaps:
            missing.append({'kind': 'code', 'source': sources[aid], 'read_with': [
                {'name': 'code.read', 'arguments': {**ref, 'offset': offset, 'limit': min(6000, b - offset)}}
                for a, b in gaps for offset in range(a, b, 6000)]})
    if missing:
        error = ValueError('Definition review still lacks actual submitted document or code ranges')
        error.missing_read_targets = missing
        error.covered_chars = count
        raise error
    return True


def request_citation(engine, run_id, packet):
    from . import independent_full_review as full
    from .document_index import load_current
    from .implementation_alignment_evidence import _required_ranges
    from .implementation_reading import coverage
    from .implementation_acceptance_flow import validate_refs

    context, result, hid = packet['context'], packet['result'], packet['handoff_id']
    architecture = context['architecture_document']
    identity = architecture['document_id'], architecture['sha256']
    validate_refs(engine, run_id, hid, context, result['source_refs'], field='source_refs')
    vote = full.stamp_vote(packet['role'], result, hid, context[full.KEY])
    owners = full.reading_handoffs(engine.store, run_id, vote)
    _, text = load_current(engine, run_id, *identity)
    refs = [ref for owner in owners[1:] for ref in engine.store.get('decisions', owner)['result']['source_refs']
        if (ref['document_id'], ref['document_sha256']) == identity]
    candidates, seen = [], set()
    for ref in refs:
        key = full.sha(ref)
        if key in seen:
            continue
        seen.add(key)
        spans = _required_ranges(engine, run_id, [ref], {identity[0]: identity[1]})[identity[0]]
        if coverage(engine, run_id, architecture, spans, owners, include_memory=False)['complete']:
            span = spans[0]['result']
            candidates.append({'reference': ref, 'text': text[span['char_start']:span['char_end']]})
        if len(candidates) == 3:
            break
    if candidates:
        raise full.ResponseCorrectionRequired('source_refs 缺少批准架构引用，已找到本人实际收到的候选片段。',
            [{'kind': CITATION, 'source_candidates': candidates}])


def validate_citation(context, result):
    from .independent_full_review import READING
    problem = citation(context)
    if problem is None:
        return
    original = context[READING]['previous_own_result']
    before, after = original['source_refs'], result['source_refs']
    if ({key: value for key, value in result.items() if key != 'source_refs'}
            != {key: value for key, value in original.items() if key != 'source_refs'}
            or after[:len(before)] != before or len(after) != len(before) + 1
            or after[-1] not in [candidate['reference'] for candidate in problem['source_candidates']]):
        from .engine import ResearchBlocked
        raise ResearchBlocked('补引答复须保留原表决、问题和原引用，仅追加本人已收候选中的一条架构引用。')
