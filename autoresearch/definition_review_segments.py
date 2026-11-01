"""Persist bounded, independent judgments before the definition final vote.

Only newly prepared records capture this protocol. Existing paid assignments,
their read ledgers and negative votes keep their original review protocol.
"""
from __future__ import annotations

import copy

from .storage import now

def groups(engine, run_id, document):
    from .architecture_documents import units
    from .architecture_revisions import balanced_cap
    return units(engine, run_id, document, cap=balanced_cap(document))


def spec(document, number, receipts, total):
    from .engine import digest
    return {'id': digest([document['sha256'], number, receipts]), 'number': number + 1,
        'total': total, 'ranges': [{'section_id': p['result']['section_id'],
            'title': p['result']['title'], 'start': p['result']['char_start'],
            'end': p['result']['char_end']} for p in receipts]}


def schema():
    from .architecture_definitions import _schema
    from .engine import STR
    result = _schema(vote=True)
    result['properties'].pop('reviewed_issue_ids')
    result['required'].remove('reviewed_issue_ids')
    result['properties']['rationale']['maxLength'] = 1800
    result['properties']['required_changes'] = {'type': 'array', 'maxItems': 20,
        'items': {'type': 'string', 'minLength': 1, 'maxLength': 1000}}
    result['properties']['unit_id'] = STR
    result['required'].append('unit_id')
    return result


def check_unit(engine, run_id, record, role, entry, unit, receipts):
    from .architecture_definitions import _binding, ROLES
    from .engine import ResearchBlocked
    from .implementation_documents import read_coverage
    expected = {k: v for k, v in entry.items()
                if k not in {'role', 'handoff_id', 'ranges', 'unit_number'}}
    _binding(record, expected)
    hid = entry.get('handoff_id', '')
    decision = engine.store.get('decisions', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {}).get('context', {})
    if (role not in ROLES or entry.get('role') != role
            or entry.get('unit_id') != unit['id'] or entry.get('unit_number') != unit['number']
            or entry.get('ranges') != unit['ranges']
            or entry.get('document_sha256') != record['document']['sha256']
            or decision.get('run_id') != run_id or decision.get('role') != role
            or decision.get('result') != expected
            or frozen.get('definition_segment_review_protocol') != 1
            or frozen.get('review_unit') != unit
            or frozen.get('definition_document') != record['document']
            or not read_coverage(engine, run_id, record['document'], receipts, hid)):
        raise ResearchBlocked('科学定义分段评议缺少真实同版本交付或未实际读完本组范围。')


def checked_role_units(engine, run_id, record, role):
    from .engine import ResearchBlocked
    reads = groups(engine, run_id, record['document'])
    entries = [v for v in record.get('unit_reviews', []) if v.get('role') == role]
    if len(entries) != len(reads):
        raise ResearchBlocked('科学定义分段评议仍有缺口，不能形成最终批准。')
    ordered = []
    for number, receipts in enumerate(reads):
        unit = spec(record['document'], number, receipts, len(reads))
        matches = [v for v in entries if v.get('unit_id') == unit['id']]
        if len(matches) != 1:
            raise ResearchBlocked('科学定义分段评议存在重复或不同版本的片段。')
        check_unit(engine, run_id, record, role, matches[0], unit, receipts)
        ordered.append(matches[0])
    return ordered


def check_vote_reads(engine, run_id, record, vote):
    from .engine import ResearchBlocked
    entries = checked_role_units(engine, run_id, record, vote['role'])
    frozen = engine.store.get('handoff_inputs', vote['handoff_id'], {}).get('context', {})
    if (frozen.get('definition_segment_review_protocol') != 1
            or frozen.get('definition_unit_reviews') != entries
            or frozen.get('definition_document') != record['document']):
        raise ResearchBlocked('科学定义终审缺少该角色自己的真实同版本分段意见。')
    if vote['approve'] and (vote['required_changes'] or
            any(not v['approve'] or v['required_changes'] for v in entries)):
        raise ResearchBlocked('科学定义分段仍有必改问题，不能通过汇总意见放行。')
    return True


async def review_context(engine, h, run_id, record, role, context):
    from .engine import ResearchBlocked
    document = record['document']
    reads = groups(engine, run_id, document)
    record['review_unit_count'] = len(reads)
    base = {**context, 'definition_segment_review_protocol': 1}
    for number, receipts in enumerate(reads):
        unit = spec(document, number, receipts, len(reads))
        prior = [v for v in record.get('unit_reviews', [])
                 if v.get('role') == role and v.get('unit_id') == unit['id']]
        if prior:
            if len(prior) != 1:
                raise ResearchBlocked('科学定义分段意见重复；原交付保留。')
            check_unit(engine, run_id, record, role, prior[0], unit, receipts)
            continue
        unit_context = {**base, 'review_unit': unit}
        instruction = ('独立评议同版本科学定义候选的本组范围。首次输入只有文档目录与 review_unit.ranges；'
            '先用 documents.read 实际读完本组，再按索引回查相关原合同或原始论文。'
            '逐项核对本组中的公式、轴、分布、种子、优先级、归约和停止规则，与批准机制及全部强比较保持一致。'
            '后续片段另有独立评议，不把分页、归档引用或尚未读取其他组当成作者缺少定义。'
            '实际缺项、冲突或尚不能确认的必要跨段依据须给出精确章节、原句和可执行的 required_changes。'
            '本组意见会单独保存并进入你自己的终审；其他角色意见不会注入。'
            '未解决必改问题时 approve=false。不得缩小比较范围或放宽种子、阈值、试次与资源隔离。'
            '未产生实验结果；批准本组不授权代码或实验。不得累积整份正文进上下文。'
            '返回提供的原方案、架构、作者哈希，以及 document_sha256=' + document['sha256']
            + '；unit_id=' + unit['id'])
        purpose = 'architecture_definition_section:' + unit['id']
        shape = schema()
        if base.get('design_review_policy_version') == 1:
            from . import design_review_policy as policy
            instruction += policy.REVIEW
            shape = policy.schema(shape, 'required_changes')
        hid = h._handoff(run_id, role, purpose, instruction, unit_context, shape)['id']
        record['active_review_handoff'] = {'role': role, 'handoff_id': hid,
            'unit_number': unit['number'], 'unit_count': unit['total']}
        engine.store.put('architecture_definitions', record['id'], record)
        result = await h.ask(run_id, role, purpose, instruction, unit_context, shape)
        entry = {**result, 'role': role, 'handoff_id': hid,
                 'unit_number': unit['number'], 'ranges': copy.deepcopy(unit['ranges'])}
        try:
            check_unit(engine, run_id, record, role, entry, unit, receipts)
        except ResearchBlocked as exc:
            record.update(status='blocked', reason=str(exc), invalid_review_unit=entry)
            engine.store.put('architecture_definitions', record['id'], record)
            raise
        record.setdefault('unit_reviews', []).append(entry)
        record['updated_at'] = now()
        engine.store.put('architecture_definitions', record['id'], record)
        engine.store.event(run_id, 'architecture.definition_section_reviewed',
            '已保存具体定义的独立分段意见。', {'role': role, 'unit_number': unit['number'],
                'unit_count': unit['total'], 'document_sha256': document['sha256'],
                'approve': result['approve'] and not result['required_changes'], 'scientific_approval': False})
    return {**base, 'definition_unit_reviews': copy.deepcopy(checked_role_units(engine, run_id, record, role))}
