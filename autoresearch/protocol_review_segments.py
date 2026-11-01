"""Durable, bounded protocol audits; archived pages remain read receipts."""
import copy
import json
import re

from . import document_index
from .storage import now


VERSION = 1
CAP = 4000
FIELDS = ('baseline_params', 'proposed_params', 'ablation_params', 'search_space',
          'ablation_search_space', 'validation_seeds', 'final_test_seeds')


class ProtocolReviewPending(Exception):
    """One saved audit scope is ready for the next graph dispatch."""


def _json_section(document, text, title):
    from .engine import ResearchBlocked
    section = next((s for s in document['sections'] if s['title'] == title), None)
    match = re.search(r'```json\n([\s\S]*?)\n```', text[section['start']:section['end']]) if section else None
    if not match:
        raise ResearchBlocked('实验协议审计缺少完整结构化原文章节：' + title)
    return json.loads(match.group(1))


def documents(engine, record, design):
    """Check original bytes against the exact approved plan, never a summary."""
    from .engine import digest, ResearchBlocked
    from .experiment_plan import build_plan
    ctx = record['context']
    protocol = ctx['protocol_document']
    architecture = next(d for d in ctx['indexed_documents'] if d['kind'] == 'architecture_design')
    protocol_index, protocol_text = document_index.load(engine, record['run_id'], protocol['document_id'], protocol['sha256'])
    architecture_index, architecture_text = document_index.load(engine, record['run_id'], architecture['document_id'], architecture['sha256'])
    for doc, manifest, text in ((protocol, protocol_index, protocol_text), (architecture, architecture_index, architecture_text)):
        if (doc['sections'] != manifest['sections'] or doc['total_chars'] != len(text)
                or doc['kind'] != manifest['document_kind']):
            raise ResearchBlocked('实验协议原文目录与完整文件不一致；不能缩减审计范围。')
    run = engine.store.get('runs', record['run_id'])
    expected = {'experiment_plan': build_plan(design, run['config']['max_trials']),
        'acceptance': design['acceptance'], 'iteration_policy': design['iteration_policy'],
        'task_primary_threshold': run['config'].get('min_relative_improvement', .01)}
    if (digest(design) != record['design_hash']
            or record['proposal_hash'] != digest(engine.store.get('consensus', record['run_id'])['proposal'])
            or digest({'plan': expected['experiment_plan'], **{k: v for k, v in expected.items() if k != 'experiment_plan'}}) != record['revision_hash']
            or any(_json_section(protocol, protocol_text, field) != value for field, value in expected.items())
            or any(_json_section(architecture, architecture_text, field) != design[field]
                   for field in FIELDS if field in design)):
        raise ResearchBlocked('实验协议分段审计的原文、批准架构或版本绑定发生变化。')
    return protocol, architecture


def units(protocol, architecture, design):
    """Every protocol character and every relevant approved parameter field."""
    from .engine import digest
    from .architecture_revisions import balanced_cap
    result = []
    for doc, selected in ((protocol, protocol['sections']),
                          (architecture, [s for s in architecture['sections'] if s['title'] in FIELDS and s['title'] in design])):
        cap = min(CAP, balanced_cap(doc))
        ranges, chars = [], 0
        for section in selected:
            cursor = section['start']
            while cursor < section['end']:
                end = min(section['end'], cursor + cap - chars)
                ranges.append({'document_id': doc['document_id'], 'expected_sha256': doc['sha256'],
                    'section_id': section['id'], 'title': section['title'], 'start': cursor, 'end': end})
                chars += end - cursor
                cursor = end
                if chars == cap:
                    result.append({'ranges': ranges, 'document_kind': doc['kind']})
                    ranges, chars = [], 0
        if ranges:
            result.append({'ranges': ranges, 'document_kind': doc['kind']})
    for index, unit in enumerate(result):
        unit.update(id=digest([protocol['sha256'], architecture['sha256'], unit]),
                    number=index + 1, total=len(result))
    return result


def prepare(engine, run_id, *, expected_record_sha256):
    """Explicit one-time migration of a completed broad review, no vote edits."""
    from .engine import ResearchBlocked, digest
    from .protocol_review_recovery import COLLECTION, checked_votes
    run = engine.store.get('runs', run_id)
    candidates = [r for r in engine.store.list(COLLECTION, run_id)
                  if r['cycle'] == engine.cycle(run_id) and r['status'] == 'blocked']
    if run['status'] not in {'blocked', 'paused'} or run['stage'] != 'implementation' or len(candidates) != 1:
        raise ResearchBlocked('须在停止的代码实现入口准备唯一待处理协议。')
    record = candidates[0]
    if digest(record) != expected_record_sha256:
        raise ResearchBlocked('原协议意见已变化；没有准备新审计。')
    if record.get('segmented_review'):
        raise ResearchBlocked('本协议已有分段审计；沿用其原交接，不重复准备。')
    if any((r.get('checkpoint') or {}).get('status') in {'queued', 'starting', 'running', 'waiting'}
           for r in engine.store.list('codex_reasoning', run_id)):
        raise ResearchBlocked('仍有活动请求，不能迁移协议审计。')
    votes = checked_votes(engine, record)
    design = engine.outputs(run_id)['design']
    protocol, architecture = documents(engine, record, design)
    from .protocol_review_recovery import accepted
    roles = [v['role'] for v in votes if not accepted(v, record['context'])]
    if not roles:
        raise ResearchBlocked('没有需要分段核对的未通过角色。')
    record['segmented_review'] = {'version': VERSION, 'status': 'reviewing', 'roles': roles,
        'source_record_sha256': expected_record_sha256,
        'source_votes': copy.deepcopy(votes), 'source_handoffs': copy.deepcopy(record['handoffs']),
        'documents': [copy.deepcopy(protocol), copy.deepcopy(architecture)],
        'units': units(protocol, architecture, design), 'unit_reviews': [], 'final_reviews': [],
        'prepared_at': now()}
    record.update(status='reading', updated_at=now())
    engine.store.put(COLLECTION, record['id'], record)
    engine.update_run(run_id, error='等待剩余角色分段核对同版本实验协议。')
    engine.store.event(run_id, 'experiment.protocol_segments_prepared',
        '已准备同版本分段审计，保留已批准角色意见和全部原始交付。',
        {'protocol_review_id': record['id'], 'roles': roles,
         'unit_count': len(record['segmented_review']['units'])})
    return record


def schema():
    from .engine import obj, STR
    return obj({'unit_id': STR, 'protocol_document_sha256': STR,
        'architecture_document_sha256': STR, 'approve': {'type': 'boolean'},
        'rationale': {'type': 'string', 'minLength': 1, 'maxLength': 1800},
        'required_changes': {'type': 'array', 'maxItems': 12,
            'items': {'type': 'string', 'minLength': 1, 'maxLength': 1000}}})


def check_entry(engine, record, entry, unit=None):
    from .engine import ResearchBlocked
    from .implementation_documents import read_coverage
    from .protocol_review_recovery import accepted
    segmented = record['segmented_review']
    protocol, architecture = segmented['documents']
    hid = entry['handoff_id']
    decision = engine.store.get('decisions', hid, {})
    handoff = engine.store.get('handoffs', hid, {})
    context = engine.store.get('handoff_inputs', hid, {}).get('context', {})
    expected = {k: v for k, v in entry.items() if k not in {'role', 'handoff_id'}}
    if (entry['role'] not in segmented['roles'] or decision.get('role') != entry['role']
            or decision.get('run_id') != record['run_id'] or decision.get('result') != expected
            or handoff.get('status') != 'completed' or handoff.get('to_role') != entry['role']
            or context.get('protocol_segment_review') != VERSION
            or any(context.get(k) != record[k] for k in ('design_hash', 'proposal_hash', 'revision_hash'))
            or context.get('protocol_document') != protocol
            or context.get('architecture_document') != architecture
            or context.get('previous_role_opinion') != next(v for v in segmented['source_votes'] if v['role'] == entry['role'])):
        raise ResearchBlocked('实验协议分段意见缺少同版本真实交付。')
    if unit is None:
        own = [v for v in segmented['unit_reviews'] if v['role'] == entry['role']]
        if context.get('section_reviews') != own or not accepted(entry, record['context']):
            raise ResearchBlocked('实验协议终审尚未闭合本角色的全部分段意见。')
        return
    if (context.get('review_unit') != unit or entry.get('unit_id') != unit['id']
            or entry.get('protocol_document_sha256') != protocol['sha256']
            or entry.get('architecture_document_sha256') != architecture['sha256']):
        raise ResearchBlocked('实验协议分段意见的范围或文档哈希不匹配。')
    for doc in (protocol, architecture):
        required = [{'result': {'section_id': r['section_id'], 'char_start': r['start'], 'char_end': r['end']}}
                    for r in unit['ranges'] if r['document_id'] == doc['document_id']]
        if required and not read_coverage(engine, record['run_id'], doc, required, hid):
            raise ResearchBlocked('实验协议分段审计未实际读完指定原文；未授予批准。')


def checked(engine, record, design, *, complete=False):
    from .engine import ResearchBlocked
    from .protocol_review_recovery import accepted, checked_votes
    segmented = record['segmented_review']
    source = checked_votes(engine, {**record, 'votes': segmented['source_votes'],
                                   'handoffs': segmented['source_handoffs']})
    if segmented['roles'] != [v['role'] for v in source if not accepted(v, record['context'])]:
        raise ResearchBlocked('实验协议分段审计不能删除原未决角色或改写原批准。')
    protocol, architecture = documents(engine, record, design)
    expected = units(protocol, architecture, design)
    if (segmented['version'] != VERSION or segmented['documents'] != [protocol, architecture]
            or segmented['units'] != expected):
        raise ResearchBlocked('实验协议分段范围已变化；不能跳过原文审计。')
    seen = set()
    for entry in segmented['unit_reviews']:
        unit = next((u for u in expected if u['id'] == entry['unit_id']), None)
        identity = (entry['role'], entry['unit_id'])
        if not unit or identity in seen:
            raise ResearchBlocked('实验协议分段意见重复或范围未知。')
        check_entry(engine, record, entry, unit)
        if entry['approve'] is not True or entry['required_changes']:
            raise ResearchBlocked('实验协议分段仍有必改问题；停止当前版本推进。')
        seen.add(identity)
    for final in segmented['final_reviews']:
        check_entry(engine, record, final)
    if complete and (len(seen) != len(expected) * len(segmented['roles'])
                     or {v['role'] for v in segmented['final_reviews']} != set(segmented['roles'])
                     or len(segmented['final_reviews']) != len(segmented['roles'])):
        raise ResearchBlocked('实验协议缺少完整分段覆盖或独立终审。')
    return expected


async def advance(engine, h, run_id, record):
    """One scoped audit per dispatch; do not loop reviewer rounds in one call."""
    from .engine import CYCLE_VOTE, Harness, ResearchBlocked
    from .protocol_review_recovery import COLLECTION, accepted, checked_votes
    design = await engine._read_consensus_check(run_id, engine.outputs, run_id)
    expected = await engine._read_consensus_check(run_id, checked, engine, record, design['design'])
    segmented = record['segmented_review']
    protocol, architecture = segmented['documents']
    source = {v['role']: v for v in segmented['source_votes']}
    for role in segmented['roles']:
        own = [v for v in segmented['unit_reviews'] if v['role'] == role]
        unit = next((u for u in expected if not any(v['unit_id'] == u['id'] for v in own)), None)
        if unit is None and any(v['role'] == role for v in segmented['final_reviews']):
            continue
        context = {**copy.deepcopy(record['context']), 'protocol_segment_review': VERSION,
            'protocol_document': protocol, 'architecture_document': architecture,
            'previous_role_opinion': copy.deepcopy(source[role])}
        context['indexed_documents'] = [protocol, architecture, *[
            d for d in context['indexed_documents'] if d['document_id'] not in {protocol['document_id'], architecture['document_id']}]]
        if unit:
            context['review_unit'] = unit
            purpose = record['purpose'] + ':segments-v1:' + unit['id']
            instruction = ('审计同版本预定执行协议的本组原文。首次只有索引和 review_unit.ranges，'
                '必须先用 documents.read 读完指定范围，再对本组实际条款给出独立意见。'
                '本组最多4000字符；每文档6000字符、总计12000字符的窗口约束不变。'
                '协议中其余配置和批准架构字段由其他组分别核对，本组不要要求全包同时可见。'
                '只审查冻结参数复制、方法/种子覆盖、禁止隐藏搜索和已批准验收停止规则的一致性；'
                '不重开已经通过的科学定义，不要求尚未批准执行的测量。'
                '跨章节决定性依据可按索引补读；明确真正不一致的字段与修复方法。'
                '本组读完且没有实质问题后保存范围内结论；原文已归档不抹除本组实际读取回执。'
                '返回unit_id及两个文档SHA256原值。')
            shape = schema()
        else:
            context['section_reviews'] = copy.deepcopy(own)
            purpose = record['purpose'] + ':segments-v1:final'
            instruction = ('根据你本人同版本有效分段审计形成最终独立意见，返回原proposal_hash与revision_hash。'
                'section_reviews均绑定你实际读取的原文范围与真实交付，宿主已校验全部指定范围。'
                '汇总所有分段参数、种子和计账结论，核对previous_role_opinion的未决事项是否闭合。'
                '无需从头重读全部原文或要求全包同时可见；有决定性跨段矛盾时定向回读。'
                '保持批准设计、阈值、冻结计划次数上限及原资源预算；本次批准不代表未来实验结果。'
                '有实质未决问题仍不批准并完整列出，不能引用其他角色批准代替自己的核验。')
            shape = CYCLE_VOTE
        from . import comparison_task_policy as comparison
        instruction, context, shape = comparison.assignment(engine, run_id, role, purpose,
            instruction, context, shape, comparison.REVIEW_GUIDANCE)
        hid = Harness.decision_key(run_id, role, purpose, instruction, context, shape)
        result = await h.ask(run_id, role, purpose, instruction, context, shape)
        entry = {**result, 'role': role, 'handoff_id': hid}
        if not unit and not accepted(entry, record['context']):
            segmented['final_reviews'].append(entry)
            record.update(status='blocked', updated_at=now())
            engine.store.put(COLLECTION, record['id'], record)
            raise ResearchBlocked('工程师协议终审仍有未决意见；已保存具体问题。')
        await engine._read_consensus_check(run_id, check_entry, engine, record, entry, unit)
        segmented['unit_reviews' if unit else 'final_reviews'].append(entry)
        record['updated_at'] = now()
        engine.store.put(COLLECTION, record['id'], record)
        engine.store.event(run_id, 'experiment.protocol_scope_reviewed',
            '已保存同版本原文分段审计；下一图调度继续剩余范围。',
            {'protocol_review_id': record['id'], 'role': role,
             'scope_id': unit['id'] if unit else 'final', 'approve': result['approve']})
        if result['approve'] is not True or result.get('required_changes'):
            record.update(status='blocked')
            engine.store.put(COLLECTION, record['id'], record)
            raise ResearchBlocked('实验协议分段仍有必改问题；已保存具体意见。')
        raise ProtocolReviewPending()
    await engine._read_consensus_check(run_id, checked, engine, record, design['design'], complete=True)
    votes = {v['role']: v for v in checked_votes(engine, record)}
    handoffs = copy.deepcopy(record['handoffs'])
    for final in segmented['final_reviews']:
        votes[final['role']] = {k: v for k, v in final.items() if k != 'handoff_id'}
        handoffs[final['role']] = final['handoff_id']
    from .engine import ROLES
    record.update(status='approved', votes=[votes[r] for r in ROLES], handoffs=handoffs, updated_at=now())
    segmented['status'] = 'approved'
    engine.store.put(COLLECTION, record['id'], record)
    return record['votes'], handoffs
