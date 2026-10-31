"""One immutable successor for a confirmed document-continuity failure.

An incomplete author candidate is retained, including every scientific concern.
This recovery grants no scientific approval and never replays an unknown call.
"""
from __future__ import annotations

import copy

from . import architecture_revisions as revisions, document_index, document_read_window
from .storage import now


def prepare(engine, run_id):
    from .engine import Harness, ResearchBlocked, digest
    run, record = engine.store.get('runs', run_id), revisions.state(engine, run_id)
    if record and record.get('working_notes_handoff'):
        return record['working_notes_handoff']
    if not record or run['mode'] != 'live' or run['status'] not in {'blocked', 'failed', 'paused'}:
        raise ResearchBlocked('阅读连续性恢复须有已停止的真实架构修订任务。')
    revisions._available(engine, run_id)
    original = engine.store.get('steps', run_id + ':design', {}).get('output')
    revisions._checked(engine, run_id, original)
    transient = {'running', 'queued', 'starting', 'turn_start_dispatched',
                 'thread_start_dispatched', 'thread_resume_dispatched'}
    if any((row.get('checkpoint') or {}).get('status') in transient
           for row in engine.store.list('codex_reasoning', run_id)):
        raise ResearchBlocked('仍有活动原请求，不能建立阅读连续性恢复。')
    pending = record.get('pending') or {}
    context = pending.get('context') or {}
    if record['status'] != 'drafting' or not document_read_window.enabled(context):
        raise ResearchBlocked('只能恢复已完成但未闭合意见的窗口阅读作者；不能覆盖独立科学否决。')
    hid = Harness.decision_key(run_id, 'engineer', pending['purpose'],
        pending['instruction'], context, pending['schema'])
    decision, assignment = engine.store.get('decisions', hid, {}), engine.store.get('handoffs', hid, {})
    result = decision.get('result') or {}
    expected = {row['id'] for row in context.get('review_fixes', [])}
    closed = set(result.get('resolved_issue_ids', []))
    if (hid != pending['handoff_id'] or decision.get('run_id') != run_id
            or decision.get('role') != 'engineer' or result.get('proposal_hash') != record['proposal_hash']
            or result.get('parent_design_hash') != context['parent_design_hash']
            or not expected or not closed < expected):
        raise ResearchBlocked('缺少确定保存且原版本绑定的未闭合作者交付，不能重发未知请求。')
    progress = engine.store.get('handoff_progress', hid, {})
    if assignment.get('document_working_notes_protocol') == 1 or progress.get('document_working_notes'):
        raise ResearchBlocked('原交接已具备阅读工作笔记，不能重复建立同类恢复。')
    pages = [row['result'] for row in progress.get('tool_results', [])
        if row.get('call', {}).get('name') == 'documents.read'
        and isinstance(row.get('result', {}).get('text'), str)]
    signatures = {(p['document_id'], p['sha256'], p['section_id'], p['char_start'], p['char_end']) for p in pages}
    view = document_read_window.project(context, progress.get('tool_results', []))
    visible = {(p['document_id'], p['sha256'], p['section_id'], p['char_start'], p['char_end'])
        for row in view['tool_results'] if row.get('call', {}).get('name') == 'documents.read'
        and isinstance((p := row.get('result', {})).get('text'), str)}
    evicted = sum((p['document_id'], p['sha256'], p['section_id'], p['char_start'], p['char_end']) not in visible
        for row in view['tool_results'] if 'document_read_reference' in (p := row.get('result', {})))
    if not evicted or len(pages) <= len(signatures):
        raise ResearchBlocked('没有原文移出窗口和重复读取的证据，不能归因为连续性故障。')
    directories = {doc['document_id']: doc for doc in context['indexed_documents']}
    originals = {}
    for page in pages:
        did = page['document_id']
        doc = directories.get(did)
        if not doc or doc['sha256'] != page['sha256']:
            raise ResearchBlocked('原读取不属于冻结文档版本。')
        if did not in originals:
            _, originals[did] = document_index.load(engine, run_id, did, doc['sha256'])
        section = next((s for s in doc['sections'] if s['id'] == page['section_id']), None)
        start, end = page['char_start'], page['char_end']
        if (not section or not section['start'] <= start < end <= section['end']
                or originals[did][start:end] != page['text']):
            raise ResearchBlocked('原读取回执不匹配实际文档字节。')
    audit_id = run_id + ':document-continuity-handoff:1'
    if engine.store.get('stage_contexts', audit_id):
        raise ResearchBlocked('已有连续性恢复审计，不能覆盖或重复建立。')
    payload = {'pending': copy.deepcopy(pending), 'previous_result': copy.deepcopy(result),
        'original_progress': copy.deepcopy(progress), 'progress_sha256': digest(progress),
        'reason': 'bounded_document_pages_without_source_anchored_working_notes',
        'document_pages': len(pages), 'distinct_pages': len(signatures), 'evicted_pages': evicted,
        'unresolved_issue_ids': sorted(expected - closed),
        'scope': 'One fresh author candidate. Retain all concerns; no science, code or experiment approval.'}
    audit = {'id': audit_id, 'run_id': run_id, 'source': 'controller_bug_recovery', 'version': 1,
        'status': 'prepared', 'previous_handoff_id': hid, 'payload': payload,
        'sha256': digest(payload), 'created_at': now()}
    engine.store.put('stage_contexts', audit_id, audit)
    record['working_notes_handoff'] = {k: audit[k] for k in
        ('id', 'status', 'previous_handoff_id', 'sha256', 'created_at')}
    engine.store.put('architecture_revisions', record['id'], record)
    engine.store.event(run_id, 'architecture.continuity_prepared',
        '原交付与读取回执已保留；新交接将携带自己的原文锚点工作笔记。', {'audit_id': audit_id})
    return record['working_notes_handoff']


def activate(engine, h, run_id, record):
    from .engine import ResearchBlocked, digest
    queued = record.get('working_notes_handoff')
    if not queued or queued['status'] != 'prepared':
        return record
    audit = engine.store.get('stage_contexts', queued['id'], {})
    payload = audit.get('payload') or {}
    prior = payload.get('pending') or {}
    if (audit.get('sha256') != queued['sha256'] or digest(payload) != queued['sha256']
            or record.get('pending') != prior
            or engine.store.get('decisions', queued['previous_handoff_id'], {}).get('result') != payload['previous_result']
            or engine.store.get('handoff_progress', queued['previous_handoff_id']) != payload['original_progress']):
        raise ResearchBlocked('阅读连续性恢复目标或原记录发生变化。')
    text = '# 原作者交付与阅读连续性故障\n\n原交付未获批准；全部意见和原判断保留。\n\n'
    text += '## previous_result\n\n```json\n' + revisions.architecture_json(payload['previous_result']) + '\n```\n\n'
    text += '## controller_continuity_failure\n\n```json\n' + revisions.architecture_json({k: payload[k] for k in
        ('reason', 'document_pages', 'distinct_pages', 'evicted_pages', 'unresolved_issue_ids', 'scope')}) + '\n```\n'
    doc = revisions._save(engine, h, run_id,
        f'cycle-{engine.cycle(run_id)}/architecture/continuity-recovery/{audit["sha256"]}.md',
        text, 'architecture_continuity_recovery')
    context = {**prior['context'], 'document_working_notes_protocol': 1,
        'document_working_notes_required': True,
        'indexed_documents': [*prior['context']['indexed_documents'], doc],
        'previous_author_response_document': doc,
        'document_continuity_recovery': {'audit_id': audit['id'], 'audit_sha256': audit['sha256'],
            'previous_handoff_id': audit['previous_handoff_id'], 'unresolved_issue_ids': payload['unresolved_issue_ids']}}
    instruction = prior['instruction'] + ('\n原作者结果及连续性故障按 previous_author_response_document 索引保存。'
        '原正文和全部回执均在；归档页的位置不保存自己的阅读结论。现在在翻页前保存带已读原文锚点的 working_notes，'
        '保留公式、参数、分支、阈值、反证和未决意见，按目录完成核对后再交付，避免反复读同页。'
        '独立重新判断所有意见。旧未解决意见与 requires_new_consensus=true 均保留，不视为已解决。'
        '若确须改变批准科学方向或放宽门槛，仍返回 requires_new_consensus=true，不得为放行填造结论。'
        '本次只是新写作候选；不得写代码、实验或自行批准。')
    purpose = prior['purpose'] + ':working-notes-v1'
    hid = h._handoff(run_id, 'engineer', purpose, instruction, context, prior['schema'])['id']
    record.setdefault('reading_recoveries', []).append({'audit_id': audit['id'],
        'pending_sha256': digest(prior), 'next_handoff_id': hid, 'created_at': now()})
    record.update(status='drafting', pending={'purpose': purpose, 'instruction': instruction,
        'context': context, 'schema': prior['schema'], 'handoff_id': hid},
        working_notes_handoff={**queued, 'status': 'applied', 'next_handoff_id': hid})
    engine.store.put('architecture_revisions', record['id'], record)
    return record
