"""Complete targeted novelty evidence before reconsidering the scientific idea."""
from __future__ import annotations

import copy
import re

from .storage import now


def evidence_keys(engine, run_id, *, note_ids=None):
    """New valid passages, not a rewritten summary or a repeated note save."""
    from .literature_notes import notes_view
    keys = set()
    for note in notes_view(engine.store, run_id, document_loader=engine.sources.document):
        if note_ids is not None and note['id'] not in note_ids:
            continue
        keys.update((note['paper_id'], note['text_sha256'], e['char_start'], e['char_end'])
                    for e in note['citations'] if e['kind'] == 'excerpts')
        # Reused RAG notes have no current-task read events. Their already
        # validated exact passages are still part of the progress baseline.
        if note.get('generation') == 'reused_ai_extraction':
            document = engine.sources.document(run_id, note['source_id'])
            for excerpt in note['excerpts']:
                match = re.fullmatch(re.escape(note['source_id']) + r'#chars=(\d+):(\d+)', excerpt['locator'])
                if match:
                    start, end = map(int, match.groups())
                else:
                    match = re.fullmatch(re.escape(note['source_id']) + r'#page=(\d+)', excerpt['locator'])
                    pages = [p for p in document.get('pages', []) if match and
                             p.get('page_number', p.get('page')) == int(match[1])]
                    if len(pages) != 1:
                        continue
                    start, end = pages[0]['start'], pages[0]['end']
                if 0 <= start < end <= len(document['text']):
                    pattern = r'\s+'.join(re.escape(part) for part in excerpt['quote'].split())
                    for found in re.finditer(pattern, document['text'][start:end]):
                        keys.add((note['paper_id'], note['text_sha256'], start + found.start(), start + found.end()))
    return sorted(keys)


def added_keys(before, after):
    """An already covered quote/subrange is not new evidence."""
    added = []
    for row in after:
        cursor, end = row[2:]
        for old in sorted((item for item in before if item[:2] == row[:2]), key=lambda item: item[2]):
            if old[3] <= cursor:
                continue
            if old[2] > cursor:
                break
            cursor = max(cursor, old[3])
            if cursor >= end:
                break
        if cursor < end:
            added.append(row)
    return sorted(added)


def legacy_failures(engine, run_id):
    """One bounded recovery for old rounds exhausted by acquisition failures."""
    from .innovation_review import attempt
    prefix = f'literature_reading:novelty-evidence:{engine.cycle(run_id)}:{attempt(engine.store, run_id)}:'
    failures = []
    for handoff in engine.store.list('handoffs', run_id):
        if not handoff.get('purpose', '').startswith(prefix):
            continue
        progress = engine.store.get('handoff_progress', handoff['id'], {})
        capped = 'tool-turn limit' in progress.get('finalization', {}).get('reason', '')
        for row in progress.get('tool_results', []):
            call, result = row.get('call', {}), row.get('result', {})
            if call.get('name') == 'literature.record_notes' and isinstance(result, dict) and result.get('error'):
                for note in call.get('arguments', {}).get('notes', []):
                    failures.append({'handoff_id': handoff['id'], 'paper_id': note.get('paper_id'),
                        'source_id': note.get('source_id'), 'error': result['error'], 'accepted': False})
        if capped:
            failures.append({'handoff_id': handoff['id'], 'reason': 'tool_turn_limit', 'accepted': False})
    return failures[:12]


async def supplement(engine, harness, run_id, result, gate, number):
    from .engine import LITERATURE_READING, STR, obj, ResearchBlocked, Harness
    from . import innovation_review, innovation_routing, literature_rounds, literature_scope
    session = engine.store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}')
    progress_key = f'{run_id}:novelty-supplement-progress:{session["cycle"]}:{session["attempt"]}:{number}'
    progress = engine.store.get('stage_contexts', progress_key)
    if not progress:
        progress = {'id': progress_key, 'run_id': run_id, 'kind': 'novelty_supplement_progress',
            'before': evidence_keys(engine, run_id), 'evidence_key_protocol': 2,
            'status': 'pending', 'created_at': now()}
        engine.store.put('stage_contexts', progress_key, progress)
    elif progress.get('evidence_key_protocol') != 2:
        historical = evidence_keys(engine, run_id, note_ids={n['id'] for n in result.get('notes', [])})
        progress.update(before=sorted(set(map(tuple, progress['before'])) | set(map(tuple, historical))),
                        evidence_key_protocol=2, baseline_upgraded_at=now())
        engine.store.put('stage_contexts', progress_key, progress)
    feedback = literature_rounds.persist_index(engine, harness.intelligence, run_id,
        f'novelty-evidence-issues-{session["attempt"]}-{number}', gate)
    context = {'literature_protocol': 2, 'literature_notes_protocol': 1,
        'source_tool_protocol': 1, 'pdf_tools_protocol': 1, 'paper_rag_protocol': 1,
        'selected_idea': session['proposal'], 'proposal_hash': session['proposal_hash'],
        'related_work_index': result['literature_index'], 'required_literature_index': result['literature_index'],
        'rejection_index': feedback, 'objective': engine.store.get('runs', run_id)['objective'],
        'acquisition_failures': legacy_failures(engine, run_id),
        'known_papers': [{k: p.get(k) for k in ('id', 'title', 'doi', 'fulltext_url')} for p in result.get('papers', [])]}
    instruction = ('当前候选尚未被证伪，先解决rejection_index中的具体阅读缺口。通过artifacts.read读取索引，'
        '优先查持久RAG；按审查中指定近邻、章节、定理或实验定向检索与阅读，必要时下载原文，'
        '使用literature.record_notes保存真实摘录、定位与分析。保持CCF A/B范围，经典近邻不限制年代。'
        '返回更新的完整文献分析和reading_plan/related_papers，保留原有核心论文和未解决问题。'
        '不能只重述缺口、不能把未找到写成不存在、不能编造实验。只用本任务已核验的论文ID。'
        '本轮集中解决最多两项具体文献缺口，不重新开展泛化综述，不例行重载已有全文。'
        '审查点名论文时用准确标题检索，先核实DOI与正式出版，再read_papers和按需阅读；'
        'metadata_mismatch中的canonical_title是下一步核验线索，不是访问失败。'
        '普通相关RAG命中不能代替找到指定近邻。每次record_notes必须检查成功回执。'
        'sources.fetch取得的arXiv HTML/附录须先literature.link_source关联到已核验正式paper_id再保存笔记，'
        '保留预印本与正式版本差异；代码证据保存在独立来源，不假冒论文正文。'
        '设计修订和未来实验验收要求保留为后续问题，不试图靠泛搜解决。')
    if number == 'mechanism-1':
        context['source_access_limits'] = [{
            'paper_id': p['id'], 'title': p['title'],
            'reading': {k: access.get(k) for k in ('retrieval_status', 'source_unavailable_code', 'fallback_status', 'error')}}
            for p in result.get('papers', [])
            if (access := engine.store.get('run_evidence', f'{run_id}:{p["id"]}', {}).get('reading', {}))
            .get('source_unavailable_code') == 'legal_fulltext_unavailable']
        context['public_version_clues'] = [row for row in engine.store.list('stage_contexts', run_id)
                                          if row.get('kind') == 'novelty_discovery_access_probe']
        context['targeted_mechanism_discovery'] = {'version': 1,
            'questions': [q for vote in gate.get('votes', []) for q in vote.get('required_changes', [])
                if re.search(r'时变谐波|振荡状态|时间重参数化|最接近.{0,8}完整', q)],
            'scope': 'One focused discovery for the closest complete available method, not another general review.'}
        instruction += ('本次唯一目标是targeted_mechanism_discovery里的完整机制近邻。先执行literature.rag_search，'
            '检索时变谐波、振荡状态、时间重参数化、历史可预测节律漂移的架构与创新机制；'
            '命中的合格已读分析直接复用，仅在缺少对应完整方法时进行网页/学术搜索。'
            '最多选两篇最接近的、可获得正文的CCF A/B方法，核验正式出版身份后补读决定性段落并保存笔记。'
            '按历史条件、未来速度推进、相位积分、谐波耦合、包络约束和适用范围比较原文实际写明的机制。'
            '复用本任务已保存且仍有效的机制证据，不重写已有摘要来冒充新增完整近邻。'
            'source_access_limits中的来源已有合法获取失败记录，不重复请求同一出版社页面；'
            'public_version_clues仅是公开检索线索，标题、作者或版本关系未核验的候选不能充当已核验正式论文原文。'
            '找不到可核验原文就明确检索范围和访问限制，不反复访问跳过来源，不宣称不存在近邻。')
    schema = obj({**LITERATURE_READING['properties'], 'related_papers': {'type': 'array', 'minItems': 1, 'maxItems': 12,
        'items': obj({key: STR for key in ('paper_id', 'role', 'relevance_to_idea', 'overlap', 'difference', 'required_check')})}})
    purpose = f'literature_reading:novelty-evidence:{session["cycle"]}:{session["attempt"]}:{number}'
    instruction, context, schema = innovation_routing.assignment(engine, run_id, purpose, instruction, context, schema)
    await innovation_review.seed_reading(engine, harness, run_id, purpose, instruction, context, schema, roles=('scout',))
    analysis = await harness.ask(run_id, 'scout', purpose, instruction, context, schema)
    handoff = Harness.decision_key(run_id, 'scout', purpose, instruction, context, schema)
    papers = engine._current_literature_papers(run_id, result.get('papers', []))
    analysis, papers, handoff = await literature_scope.reconcile(engine, harness, run_id, analysis, papers,
        source_handoff_id=handoff, context=context, schema=schema)
    # A repair cannot make an objection disappear by dropping the original work.
    analysis = copy.deepcopy(analysis)
    related = {p['paper_id']: p for p in result['analysis'].get('related_papers', [])}
    related.update({p['paper_id']: p for p in analysis['related_papers']})
    analysis['related_papers'] = list(related.values())
    plan = analysis['reading_plan']
    plan['core_paper_ids'] = sorted(set(plan['core_paper_ids']) | set(related)
        | set(result['reading_gate'].get('selected_core_ids', result['reading_gate']['noted_core_ids'])))
    reading = engine._save_literature_reading(run_id, analysis, papers)
    added = added_keys(list(map(tuple, progress['before'])), evidence_keys(engine, run_id))
    targeted = []
    from . import novelty_reading
    if (not reading['reading_gate']['approved'] or not added
            or engine.store.get('stage_contexts', progress_key + ':local-reading-v1')):
        targeted = await novelty_reading.complete(engine, harness, run_id,
            {**result, 'analysis': analysis, 'papers': papers, **reading}, gate, progress_key, handoff)
        for job in targeted:
            if job['status'] not in {'completed', 'reused'}:
                continue
            pid = job['paper_id']
            if pid not in {row['paper_id'] for row in analysis['related_papers']}:
                analysis['related_papers'].append({'paper_id': pid, 'role': '审查指定近邻',
                    'relevance_to_idea': '审查要求核对的完整机制近邻，原文片段已保存。',
                    'overlap': '需根据新增原文笔记独立审查，不由读取控制器判定。',
                    'difference': '需根据新增原文笔记独立审查，不宣称创新已通过。',
                    'required_check': '依据保存的补充笔记核对机制、适用条件和未读范围。'})
                analysis['paper_ids'] = list(dict.fromkeys([*analysis['paper_ids'], pid]))
                analysis['reading_plan']['core_paper_ids'] = sorted(set(analysis['reading_plan']['core_paper_ids']) | {pid})
        reading = engine._save_literature_reading(run_id, analysis, papers)
        added = added_keys(list(map(tuple, progress['before'])), evidence_keys(engine, run_id))
    if not reading['reading_gate']['approved']:
        reading = await engine._complete_literature_reading(run_id, harness, context, analysis, papers,
            reading, source_handoff_id=handoff, progress_baseline=engine._reading_material_keys(run_id))
        papers = engine._current_literature_papers(run_id, papers)
    if not reading['reading_gate']['approved']:
        raise ResearchBlocked('相似性核查补读尚未形成合格原文笔记；保留当前候选和补读记录，不自动判为科学否决。')
    if not added:
        engine.store.put('stage_contexts', progress_key, {**progress, 'status': 'no_new_evidence'})
        details = '；'.join(f'{job["title"]}：新增摘录{job["added_passages"]}条' for job in targeted)
        raise ResearchBlocked('定向补读未新增有效原文摘录，未消耗完成轮次或重复付费评审；'
                              '请检查具体论文获取和笔记保存错误。候选与原始回执已保留。' + details)
    engine.store.put('stage_contexts', progress_key, {**progress, 'status': 'completed',
        'added_passages': len(added), 'completed_at': now()})
    updated = {**copy.deepcopy(result), 'analysis': analysis, 'papers': papers, **reading}
    if targeted:
        updated['targeted_evidence'] = targeted
    updated.pop('novelty_gate', None)
    updated['literature_index'] = literature_rounds.persist_index(engine, harness.intelligence, run_id,
        f'related-evidence-{session["attempt"]}-{number}', {k: v for k, v in updated.items() if k != 'literature_index'})
    return updated


async def review_with_supplements(engine, harness, run_id, result, review_once):
    from .engine import ResearchBlocked
    from .innovation_review import attempt, correct_cached_route
    key = f'{run_id}:novelty-evidence:{engine.cycle(run_id)}:{attempt(engine.store, run_id)}'
    state = engine.store.get('stage_contexts', key) or {
        'id': key, 'run_id': run_id, 'kind': 'novelty_evidence', 'round': 0,
        'result': copy.deepcopy(result), 'history': [], 'evidence_protocol': 2}
    while True:
        gate = state.get('gate')
        if gate is not None:
            corrected = correct_cached_route(engine, run_id, state['result'], gate)
            if corrected != gate:
                state['gate'] = corrected
                session = engine.store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}')
                engine.store.batch([('stage_contexts', key, state),
                    ('innovation_sessions', session['id'], {**session, 'novelty_gate': corrected})])
                engine.store.event(run_id, 'innovation.route_corrected',
                    corrected['route_correction']['reason'],
                    {'attempt': corrected['attempt'], 'proposal_hash': corrected['proposal_hash'],
                     'previous_route': gate['route'], 'route': corrected['route']})
                gate = corrected
        if gate is None:
            gate = await review_once(engine, harness, run_id, state['result'],
                supplement=state.get('review_supplement',
                    'repair-1' if state.get('acquisition_repair', {}).get('completed') else state['round']))
            state['gate'] = gate
            engine.store.put('stage_contexts', key, state)
        result.clear()
        result.update(copy.deepcopy(state['result']))
        if gate['approved'] or gate['route'] != 'supplement_evidence':
            return gate
        if state['round'] >= 2:
            repair = state.get('acquisition_repair')
            if not state.get('evidence_protocol') and not repair:
                failures = legacy_failures(engine, run_id)
                if failures:
                    repair = {'version': 1, 'completed': False, 'failures': failures, 'created_at': now()}
                    state['acquisition_repair'] = repair
                    engine.store.put('stage_contexts', key, state)
                    engine.store.event(run_id, 'innovation.acquisition_repair',
                        '旧补读存在工具中断或笔记保存失败，启用一次定向获取修复；保留原两轮和评审记录。',
                        {'attempt': gate.get('attempt'), 'version': 1})
            if repair and not repair['completed']:
                updated = await supplement(engine, harness, run_id, state['result'], gate, 'repair-1')
                state['history'].append({'round': 'repair-1', 'result': state['result'],
                    'gate': gate, 'completed_at': now()})
                state.update(result=updated, gate=None,
                    acquisition_repair={**repair, 'completed': True, 'completed_at': now()})
                engine.store.put('stage_contexts', key, state)
                continue
            if repair:
                raise ResearchBlocked('定向获取修复已完成，独立审查仍有具体未解决问题；'
                                      '补读与评审已保存，不重复请求或自动放行实验。')
            # Two broad supplement rounds are a guard against repeated summaries,
            # not a lifetime ban on reading a newly located decisive passage.
            # Each source/page footprint gets one journaled targeted job. Only
            # newly accepted excerpts can buy another independent review.
            if await targeted_extension(engine, harness, run_id, state, key, gate):
                continue
            from . import novelty_discovery
            if await novelty_discovery.complete(engine, harness, run_id, state, key, gate):
                continue
            raise ResearchBlocked('相似性审查两轮定向补读后仍有证据缺口；保留当前候选，未将缺证据当作创新已否决。')
        updated = await supplement(engine, harness, run_id, state['result'], gate, state['round'] + 1)
        state['history'].append({'round': state['round'], 'result': state['result'], 'gate': gate, 'completed_at': now()})
        state.update(result=updated, round=state['round'] + 1, gate=None)
        engine.store.put('stage_contexts', key, state)


async def targeted_extension(engine, harness, run_id, state, state_key, gate):
    from .engine import ResearchBlocked, digest
    from . import novelty_reading, literature_rounds
    await novelty_reading.adopt_section_sources(engine, harness, run_id, state['result'], gate)
    extensions = state.setdefault('targeted_extensions', [])
    pending = next((row for row in extensions if row['status'] == 'pending'), None)
    if pending is None:
        # A dispatched/completed unchanged footprint cannot be sent again merely
        # because a reviewer rephrased the same objection or the user resumed.
        used = {digest([t['paper_id'], t['text_sha256'], call])
                for row in extensions for t in row['targets'] for call in t['read']}
        before = evidence_keys(engine, run_id)
        targets = []
        for target in novelty_reading.local_targets(engine, run_id, state['result'], gate, limit=None):
            if not target.get('current_request'):
                continue
            target['read'] = [call for call in target['read']
                if digest([target['paper_id'], target['text_sha256'], call]) not in used]
            if target['read'] and not novelty_reading._already_covered(engine, run_id, target, before):
                targets.append(target)
            if len(targets) == 2:
                break
        if not gate.get('votes') or not targets:
            return False
        number = len(extensions) + 1
        pending = {'id': state_key + f':targeted-{number}', 'status': 'pending',
            'number': number, 'targets': targets, 'before': before, 'created_at': now()}
        extensions.append(pending)
        engine.store.put('stage_contexts', state_key, state)
    progress_key = pending['id']
    jobs = await novelty_reading.complete(engine, harness, run_id, state['result'], gate,
        progress_key, state_key, targets=pending['targets'])
    added = added_keys(list(map(tuple, pending['before'])), evidence_keys(engine, run_id))
    if not added:
        pending.update(status='no_new_evidence', jobs=jobs, completed_at=now())
        engine.store.put('stage_contexts', state_key, state)
        raise ResearchBlocked('定向补读未新增有效原文摘录，未消耗完成轮次或重复付费评审；'
                              '候选、已读范围及原始回执已保留，请查看具体笔记校验或来源获取记录。')
    updated = copy.deepcopy(state['result'])
    updated.update(engine._save_literature_reading(run_id, updated['analysis'], updated['papers']))
    if not updated['reading_gate']['approved']:
        raise ResearchBlocked('相似性核查补读尚未形成合格原文笔记；保留当前候选和补读记录，不自动判为科学否决。')
    updated['targeted_evidence'] = [*updated.get('targeted_evidence', []), *jobs]
    updated.pop('novelty_gate', None)
    label = f'targeted-{pending["number"]}'
    updated['literature_index'] = literature_rounds.persist_index(engine, harness.intelligence, run_id,
        f'related-evidence-{gate.get("attempt", 1)}-{label}', updated)
    pending.update(status='completed', added_passages=len(added), jobs=jobs, completed_at=now())
    state['history'].append({'round': label, 'result': state['result'], 'gate': gate, 'completed_at': now()})
    state.update(result=updated, gate=None, review_supplement=label)
    engine.store.put('stage_contexts', state_key, state)
    engine.store.event(run_id, 'innovation.targeted_evidence_added',
        '已保存新增决定性原文片段并入RAG，继续独立相似性审查；已有分析和原始回执保留。',
        {'extension': label, 'added_passages': len(added), 'paper_ids': [j['paper_id'] for j in jobs]})
    return True
