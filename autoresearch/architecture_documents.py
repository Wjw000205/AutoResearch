"""Save architecture Markdown, index it, then review bounded document sections.

Scientific design remains the original typed object. Document/review metadata
is stored separately so it cannot recursively change the design fingerprint.
"""
from __future__ import annotations

import copy
import hashlib
import json

from . import document_index
from .architecture_validation import operation
from .storage import now

VERSION = 1
PEERS = ('director', 'scout')
GROUPS = (
    ('架构与机制', ('architecture',)), ('数据与划分', ('dataset',)),
    ('评估与隔离', ('evaluation', 'primary_metric', 'direction')),
    ('模型参数与比较矩阵', ('baseline_params', 'proposed_params', 'ablation_params',
       'search_space', 'ablation_search_space', 'validation_seeds', 'final_test_seeds', 'benchmark_tasks')),
    ('资源与依赖', ('resource_plan',)), ('验收与停止规则', ('acceptance', 'iteration_policy')),
    ('局限与未解决问题', ('limitations',)),
)


def enabled(engine, run_id):
    return engine.store.get('runs', run_id)['config'].get('architecture_document_protocol') == VERSION


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def render(design, proposal_hash, *, cycle, provenance):
    from .engine import digest
    lines = ['# 架构设计', '', f'设计版本：`{digest(design)}`',
             f'对应共识方案：`{proposal_hash}`', f'研究轮次：{cycle}',
             f'来源：{provenance}；本文件描述待验证设计，不包含实验结果。', '']
    known = set()
    for title, fields in GROUPS:
        lines += ['## ' + title, '']
        for field in fields:
            known.add(field)
            if field == 'benchmark_tasks' and field not in design:
                continue  # Do not rewrite immutable legacy architecture documents.
            if field not in design:
                lines += [f'### {field}', '', '未显式提供；不得据此推断已满足。', '']
                continue
            value = design[field]
            lines += ['### ' + field, '']
            lines += ([value, ''] if isinstance(value, str) else ['```json', _json(value), '```', ''])
    extra = {k: v for k, v in design.items() if k not in known}
    if extra:
        lines += ['## 其他设计字段', '', '```json', _json(extra), '```', '']
    return '\n'.join(lines)


def save(engine, harness, run_id, design):
    from .engine import digest
    consensus = engine.store.get('consensus', run_id, {})
    proposal = consensus.get('proposal') or engine.outputs(run_id).get('debate', {}).get('proposal')
    ph = digest(proposal)
    run, cycle = engine.store.get('runs', run_id), engine.cycle(run_id)
    dh = digest(design)
    base = f'cycle-{cycle}/architecture/{dh}'
    content = render(design, ph, cycle=cycle, provenance=run['mode'])
    if 'benchmark_tasks' in design:
        from .experiment_protocols import resolve_design
        protocols = resolve_design(engine, run_id, design)
        content += '\n\n## 论文实验参数与原文来源\n\n以下为已读片段的结构化提取，仍须核对适用设置与语义；保存不代表科学审批。\n\n```json\n' + _json(protocols) + '\n```\n'

    def immutable(name, text, kind):
        existing = engine.store.get('artifacts', run_id + ':' + name)
        if existing:
            _, old = document_index.artifact_text(engine, run_id, existing['id'])
            if old != text:
                raise ValueError('Immutable architecture document content changed')
            return existing
        return engine.artifact(run_id, name, text, kind)

    artifact = immutable(base + '/architecture.md', content, 'architecture_design')
    directory = document_index.index(engine, harness.intelligence, run_id, artifact['id'], kind='architecture_design')
    approved = immutable('ideas/' + ph + '/approved-proposal.md',
        '# 已通过共识的方案\n\n方案指纹：`' + ph + '`\n\n```json\n' + _json(proposal) + '\n```\n', 'approved_proposal')
    proposal_index = document_index.index(engine, harness.intelligence, run_id, approved['id'], kind='approved_proposal')
    return {'id': f'{run_id}:architecture:{cycle}:{dh}', 'run_id': run_id, 'cycle': cycle,
            'version': VERSION, 'design_hash': dh, 'proposal_hash': ph,
            'document': directory, 'approved_proposal': proposal_index, 'provenance': run['mode']}


def units(engine, run_id, document, *, cap=None):
    """Every character inspected, but never the complete document in one input."""
    cap = min(6000, max(800, document['total_chars'] // 2)) if cap is None else cap
    rows, current, chars = [], [], 0
    for section in document['sections']:
        offset = 0
        while offset < section['chars']:
            if chars == cap:
                rows.append(current)
                current, chars = [], 0
            limit = min(cap - chars, section['chars'] - offset)
            args = {'document_id': document['document_id'], 'expected_sha256': document['sha256'],
                    'section_id': section['id'], 'offset': offset, 'limit': limit}
            page = document_index.read(engine, run_id, args)
            current.append({'call': {'name': 'documents.read', 'arguments': args}, 'result': page})
            chars += len(page['text'])
            offset += len(page['text'])
    if current:
        rows.append(current)
    return rows


def _schema(final=False):
    from .engine import obj, STR, STRS
    fields = {'document_sha256': STR, 'approve': {'type': 'boolean'},
              'rationale': {'type': 'string', 'maxLength': 1800},
              'issues': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 1000}, 'maxItems': 20}}
    fields['design_hash' if final else 'unit_id'] = STR
    return obj(fields)


def _seed(engine, harness, run_id, role, purpose, instruction, context, schema, receipts):
    """Commit exact bounded tool reads before the paid section-review request."""
    assignment = harness._handoff(run_id, role, purpose, instruction, context, schema)
    if not engine.store.get('handoff_progress', assignment['id']):
        harness._save_progress(run_id, assignment['id'], {'turn': 0, 'seen': {},
            'tool_results': copy.deepcopy(receipts)})
    return assignment['id']


def bounded_tool_result(context, receipts, call, output):
    """Supplemental reads cannot accumulate the whole Markdown in one request."""
    if (context.get('architecture_diagram_authoring_protocol') == 2
            and call.get('name') == 'architecture.draw_diagrams'):
        return output  # Compact tool save receipt, not additional document text.
    if context.get('indexed_document_context') is True:
        from .implementation_documents import bounded_tool_result as bounded
        return bounded(context, receipts, call, output)
    if not (context.get('indexed_document_review') is True and context.get('document_index_protocol') == 1):
        return output
    if call.get('name') not in {'documents.list', 'documents.search', 'documents.read', 'hardware.inspect', 'resources.assess'}:
        raise ValueError('Indexed document review requires its scoped document tools')
    if call.get('name') != 'documents.read' or not isinstance(output, dict) or output.get('error'):
        return output
    pages = [p['result'] for p in receipts if p.get('call', {}).get('name') == 'documents.read'
             and isinstance(p.get('result'), dict) and isinstance(p['result'].get('text'), str)]
    used = sum(len(page['text']) for page in pages)
    doc = next((d for d in (context.get('architecture_document'), context.get('approved_proposal'))
                if isinstance(d, dict) and d.get('document_id') == output.get('document_id')), None)
    same_doc_chars = sum(len(page['text']) for page in pages if page.get('document_id') == output.get('document_id'))
    if not doc or used + len(output.get('text', '')) > 6000 or same_doc_chars + len(output.get('text', '')) >= doc['total_chars']:
        return {'error': '本次文档评议正文已达到分段范围；不得继续累积全文。使用目录、检索定位和已保存分段意见；'
                         '无法判断的跨段问题请具体列入待核实或修改意见。',
                'document_id': output.get('document_id'), 'sha256': output.get('sha256'),
                'current_document_chars': used, 'document_text_limit': 6000, 'text_returned': False}
    return output


def gate(engine, run_id, design, *, source_cycle=None):
    from .engine import ResearchBlocked, digest
    if source_cycle is not None and source_cycle != engine.cycle(run_id):
        import copy
        from .architecture_validation import operation
        reader = copy.copy(engine)
        reader.cycle = lambda _: source_cycle
        return operation(gate)(reader, run_id, design, source_cycle=source_cycle)
    if not enabled(engine, run_id):
        return
    key = f'{run_id}:architecture:{source_cycle if source_cycle is not None else engine.cycle(run_id)}:{digest(design)}'
    record = engine.store.get('architecture_reviews', key)
    if (not record or record.get('status') != 'approved' or record.get('design_hash') != digest(design)
            or record.get('proposal_hash') != digest(engine.store.get('consensus', run_id, {}).get('proposal'))):
        raise ResearchBlocked('架构 Markdown 尚未取得同一文档版本的独立评议通过；代码实现与实验不会推进。')
    document_index.load(engine, run_id, record['document']['document_id'], record['document']['sha256'])
    if record.get('review_recovery_protocol') == 1:
        from .architecture_review_recovery import gate as recovery_gate
        return recovery_gate(engine, run_id, record)
    reviews = record.get('reviews', [])
    if (len(reviews) != 2 or {v.get('role') for v in reviews} != set(PEERS)
            or any(v.get('approve') is not True or v.get('issues')
                or v.get('document_sha256') != record['document']['sha256']
                or v.get('design_hash') != record['design_hash'] for v in reviews)):
        raise ResearchBlocked('架构评议记录与当前文档不一致。')
    for review in reviews:
        decision = engine.store.get('decisions', review.get('handoff_id', ''), {})
        if (decision.get('role') != review['role'] or decision.get('run_id') != run_id
                or decision.get('result') != {k: v for k, v in review.items() if k not in {'role', 'handoff_id'}}):
            raise ResearchBlocked('架构评议缺少对应角色的真实交付记录。')
    expected_units = units(engine, run_id, record['document'], cap=record.get('unit_cap'))
    unit_reviews = record.get('unit_reviews', [])
    if len(unit_reviews) != len(expected_units) * len(PEERS):
        raise ResearchBlocked('架构文档仍有未评议片段。')
    for role in PEERS:
        for number, receipts in enumerate(expected_units):
            unit_id = digest([record['document']['sha256'], number, receipts])
            matches = [v for v in unit_reviews if v.get('role') == role and v.get('unit_id') == unit_id]
            if len(matches) != 1 or matches[0].get('approve') is not True or matches[0].get('issues'):
                raise ResearchBlocked('架构分段评议存在缺口或未解决问题。')
            vote = matches[0]
            decision = engine.store.get('decisions', vote.get('handoff_id', ''), {})
            result = {k: v for k, v in vote.items() if k not in {'role', 'handoff_id', 'ranges', 'unit_number'}}
            progress = engine.store.get('handoff_progress', vote.get('handoff_id', ''), {})
            from .implementation_documents import read_coverage
            if (decision.get('role') != role or decision.get('run_id') != run_id or decision.get('result') != result
                    or (not read_coverage(engine, run_id, record['document'], receipts, vote['handoff_id'])
                        if record.get('review_protocol') == 2 else
                        any(receipt not in progress.get('tool_results', []) for receipt in receipts))):
                raise ResearchBlocked('架构分段评议缺少对应版本的阅读与交付记录。')


async def review(engine, harness, run_id, design):
    from .engine import ResearchBlocked, digest
    if not enabled(engine, run_id):
        return None
    await engine._read_consensus_check(run_id, engine._consensus_gate, run_id)
    saved = save(engine, harness, run_id, design)
    record = engine.store.get('architecture_reviews', saved['id'])
    if record and record.get('status') == 'approved':
        await engine._read_consensus_check(run_id, operation(gate), engine, run_id, design)
        return record
    if record and record.get('status') == 'rejected':
        raise ResearchBlocked('架构文档评议未通过；具体修改意见已保存在架构设计详情中。')
    if record is None:
        from .design_review_policy import VERSION
        record = {**saved, 'created_at': now(), 'unit_reviews': [], 'reviews': [],
                  'design_review_policy_version': VERSION}
        from . import architecture_convergence as convergence
        if convergence.enabled(engine, run_id):
            record['architecture_progress_protocol'] = convergence.VERSION
        from .architecture_revisions import revised, balanced_cap
        if revised(engine, run_id, design):
            record.update(review_protocol=2, unit_cap=balanced_cap(saved['document']))
            from .architecture_revisions import state
            record['document_read_window_protocol'] = (state(engine, run_id) or {}).get('document_read_window_protocol', 0)
            if engine.store.get('runs', run_id)['config'].get('architecture_workflow_protocol') == 1:
                record.update(architecture_workflow_protocol=1, document_read_window_protocol=2)
        if engine.store.get('runs', run_id)['config'].get('architecture_review_recovery_protocol', 1) == 1:
            record.update(review_recovery_protocol=1, evidence_amendment_limit=3,
                          unit_review_amendments=[], final_review_amendments=[],
                          architecture_workflow_protocol=1, document_read_window_protocol=2)
    if record.get('review_recovery_protocol') == 1:
        from .architecture_review_recovery import advance
        return await advance(engine, harness, run_id, design, record)
    record = {**record, 'status': 'reviewing'}
    engine.store.put('architecture_reviews', saved['id'], record)
    engine.store.event(run_id, 'architecture.document_indexed', '架构 Markdown 已保存并建立文档索引，正在分段评议。',
                       {'artifact_id': saved['document']['artifact_id'], 'sha256': saved['document']['sha256']})
    read_units = units(engine, run_id, saved['document'], cap=record.get('unit_cap'))
    config = engine.store.get('runs', run_id)['config']
    from .execution_contract import constraints
    context = {'document_index_protocol': 1, 'indexed_document_review': True,
               'source_tool_protocol': 1, 'research_cycle': saved['cycle'],
               'architecture_document': saved['document'], 'approved_proposal': saved['approved_proposal'],
               'design_hash': saved['design_hash'], 'execution_constraints': constraints(config)}
    from .dataset_transition import review_scope
    context.update(review_scope(engine.store, run_id))
    from . import design_review_policy as policy
    if record.get('design_review_policy_version') == policy.VERSION:
        context.update(design_review_policy_version=policy.VERSION, design_review_issue_field='issues')
    if record.get('review_protocol') == 2:
        from .architecture_revisions import review_context
        context.update(review_context(engine, run_id, design,
            read_window_protocol=record.get('document_read_window_protocol', 0)))
        context['indexed_documents'] = [saved['document'], *[
            doc for doc in context['indexed_documents'] if doc['document_id'] != saved['document']['document_id']]]
    if record.get('architecture_workflow_protocol') == 1:
        context.update(architecture_workflow_protocol=1,
                       architecture_required_documents=[saved['document']['document_id']])
    for role in PEERS:
        sections = []
        for number, receipts in enumerate(read_units):
            unit_id = digest([saved['document']['sha256'], number, receipts])
            if record.get('architecture_workflow_protocol') == 1:
                previous = next((v for v in record['unit_reviews']
                                 if v['role'] == role and v['unit_id'] == unit_id), None)
                if previous:
                    sections.append(previous)
                    continue
            scope = [{'section_id': p['result']['section_id'], 'title': p['result']['title'],
                      'start': p['result']['char_start'], 'end': p['result']['char_end']} for p in receipts]
            unit_context = {**context, 'review_unit': {'id': unit_id, 'number': number + 1,
                                                     'total': len(read_units), 'ranges': scope}}
            instruction = ('评议已保存架构 Markdown 的本组精确片段。tool_results 是按文档索引读取的正文，'
                '当前输入仅包含本组，不是整份文档。审查本段与共识方案、科学机制、具体实现、数据隔离、'
                '比较矩阵、种子、验收阈值和资源约束是否一致。需要跨章节依据时使用 documents.search/read 按需读取，'
                '不要要求全文塞进上下文。已通过的共识仍是设计约束，不是实验结果或架构评议批准。'
                '本段若有必须修改的问题，approve=false 并在 issues 中指出章节、原句和具体修改；'
                '暂时无法确认的跨段问题也应明确记录。返回 supplied document_sha256 和 unit_id 原值。')
            instruction += '\ndocument_sha256=' + saved['document']['sha256'] + '; unit_id=' + unit_id
            purpose = 'design_document_section:' + unit_id
            schema = _schema()
            if record.get('review_protocol') == 2:
                instruction = ('首次输入只有目录和 review_unit.ranges。先按 documents.read 主动读取本组全部字符范围再评议。'
                    '与已通过共识、机制、精确实现、数据隔离、种子、完整比较、验收和停止规则逐项核对。'
                    '跨章节依据按索引补读。单份文档至多6000字符、总共12000字符；本组预留跨段阅读空间。'
                    '后续片段另行审查，分页或历史已解决的缺口不作为否决理由。确有必改问题须定位章节并提出具体修正。'
                    '当前宿主约束与明确操作授权优先于历史叙述上限；引用的论文选段不能认证全文或发表版等价。'
                    '返回 supplied document_sha256=' + saved['document']['sha256'] + '; unit_id=' + unit_id)
                if record.get('architecture_workflow_protocol') == 1:
                    instruction = instruction.replace('单份文档至多6000字符、总共12000字符',
                        '单页至多6000字符，窗口每份文档至多12000字符、合计24000字符')
            if context.get('design_review_policy_version') == policy.VERSION:
                instruction += policy.REVIEW
                schema = policy.schema(schema, 'issues')
            from . import comparison_task_policy as comparison
            instruction, unit_context, schema = comparison.assignment(engine, run_id, role, purpose,
                instruction, unit_context, schema, comparison.REVIEW_GUIDANCE)
            if record.get('review_protocol') == 2:
                hid = harness._handoff(run_id, role, purpose, instruction, unit_context, schema)['id']
            else:
                hid = _seed(engine, harness, run_id, role, purpose, instruction, unit_context, schema, receipts)
            result = await harness.ask(run_id, role, purpose, instruction, unit_context, schema)
            if result.get('document_sha256') != saved['document']['sha256'] or result.get('unit_id') != unit_id:
                raise ResearchBlocked('架构分段评议返回了不同的文档版本；原交付已保留。')
            if record.get('review_protocol') == 2:
                from .implementation_documents import read_coverage
                if not read_coverage(engine, run_id, saved['document'], receipts, hid):
                    raise ResearchBlocked('新架构分段评议未读取本组全部范围；目录不能代替实际阅读。')
            entry = {**result, 'role': role, 'handoff_id': hid, 'ranges': scope, 'unit_number': number + 1}
            sections.append(entry)
            prior = [v for v in record['unit_reviews'] if not (v['role'] == role and v['unit_id'] == unit_id)]
            record = {**record, 'unit_count': len(read_units), 'unit_reviews': [*prior, entry], 'updated_at': now()}
            engine.store.put('architecture_reviews', saved['id'], record)
            if record.get('architecture_workflow_protocol') == 1:
                return record
        if record.get('architecture_workflow_protocol') == 1 and any(v['role'] == role for v in record['reviews']):
            continue
        final_context = {**context, 'section_reviews': sections,
                         'document_sha256': saved['document']['sha256']}
        instruction = ('对同一架构 Markdown 的逐段评议形成最终独立意见。你已分别检查 section_reviews 的全部范围；'
            '当前只提供目录和你自己的评议记录，不注入文档全文或另一角色的结论。'
            '检查跨章节机制、实验矩阵、数据隔离、资源、验收和停止条件的一致性，必要时按索引补读。'
            '存在任何未解决的必改问题时不得批准，issues 必须具体可执行；通过只允许推进实现，不是科学成功。'
            '返回 supplied document_sha256 和 design_hash 原值。')
        purpose, schema = 'design_document_review:' + saved['design_hash'], _schema(final=True)
        if context.get('design_review_policy_version') == policy.VERSION:
            instruction += policy.REVIEW + '\n终审须汇总本角色全部分段必改意见，不得遗漏；跨段重复项合并并给出完整修正。'
            schema = policy.schema(schema, 'issues')
        if record.get('architecture_progress_protocol') == 1:
            from . import architecture_convergence as convergence
            final_context.update(convergence.context(engine, run_id, 'architecture', design, role=role))
            instruction += convergence.REVIEW
            schema = convergence.schema(schema)
        from . import comparison_task_policy as comparison
        instruction, final_context, schema = comparison.assignment(engine, run_id, role, purpose,
            instruction, final_context, schema, comparison.REVIEW_GUIDANCE)
        hid = harness._handoff(run_id, role, purpose, instruction, final_context, schema)['id']
        result = await harness.ask(run_id, role, purpose, instruction, final_context, schema)
        if result.get('document_sha256') != saved['document']['sha256'] or result.get('design_hash') != saved['design_hash']:
            raise ResearchBlocked('架构最终评议返回了不同的文档或设计版本。')
        prior = [v for v in record['reviews'] if v['role'] != role]
        record = {**record, 'reviews': [*prior, {'role': role, 'handoff_id': hid, **result}], 'updated_at': now()}
        engine.store.put('architecture_reviews', saved['id'], record)
        if record.get('architecture_workflow_protocol') == 1 and len(record['reviews']) < len(PEERS):
            return record
    approved = all(v['approve'] is True and not v['issues'] for v in [*record['unit_reviews'], *record['reviews']])
    record = {**record, 'status': 'approved' if approved else 'rejected', 'completed_at': now()}
    engine.store.put('architecture_reviews', saved['id'], record)
    if record.get('architecture_progress_protocol') == 1:
        from .architecture_convergence import record as record_progress
        record_progress(engine, run_id, 'architecture', record, design)
    engine.artifact(run_id, f'cycle-{saved["cycle"]}/architecture/{saved["design_hash"]}/review.json', _json(record), 'architecture_review')
    engine.store.event(run_id, 'architecture.reviewed', '架构文档评议通过，准备推进代码实现。' if approved else '架构文档评议未通过，具体修改意见已保存。',
                       {'document_sha256': saved['document']['sha256'], 'design_hash': saved['design_hash'], 'approved': approved})
    await engine._read_consensus_check(run_id, operation(gate), engine, run_id, design)
    return record


def view(engine, run_id):
    rows = engine.store.list('architecture_reviews', run_id)
    from .architecture_review_recovery import effective_source
    for index, row in enumerate(rows):
        if row.get('review_recovery_protocol') == 1:
            effective = effective_source(row)
            rows[index] = {**row, 'effective_unit_reviews': effective['unit_reviews'],
                           'effective_reviews': effective['reviews']}
    cycle = engine.cycle(run_id)
    current = [row for row in rows if row.get('cycle') == cycle]
    from .architecture_revisions import view as revision_view
    return {'current': sorted(current, key=lambda row: row.get('created_at', ''))[-1] if current else None,
            'history': sorted(rows, key=lambda row: row.get('created_at', '')),
            'revision': revision_view(engine, run_id),
            'required': enabled(engine, run_id)}
