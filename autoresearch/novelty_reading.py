"""Bounded local reading of novelty objections, including already-noted papers.

The general reading gate retains historical notes. This queue supplies a new,
paper-scoped assignment for missing passages without replaying paid summaries.
"""
from __future__ import annotations

import copy
import re

from .storage import now


def _pages(questions, document):
    available = sorted(p.get('page_number', p.get('page')) for p in document.get('pages', [])
                       if type(p.get('page_number', p.get('page'))) is int and p.get('text', '').strip())
    requested = []
    # Negative page references describe work to reuse, never a read request.
    clauses = re.split(r'[。；;\n]', '\n'.join(questions))
    text = '\n'.join(q for q in clauses if not re.search(
        r'无需(?:重新|重复)?(?:读取|阅读|读)|不再(?:重新|重复)?(?:读取|阅读|读)|'
        r'(?:do not|no need to|without)\s+(?:re[- ]?)?read', q, re.I))
    for pattern in (r'(?:第\s*)?(\d{1,3})\s*[–—−~～至到-]\s*(\d{1,3})\s*页',
                    r'pages?\s+(\d{1,3})\s*[–—−-]\s*(\d{1,3})'):
        for start, end in re.findall(pattern, text, re.I):
            requested.extend(range(int(start), min(int(end), int(start) + 3) + 1))
    requested.extend(int(n) for n in re.findall(r'第\s*(\d{1,3})\s*页', text))
    requested.extend(int(n) for n in re.findall(r'page\s+(\d{1,3})\b', text, re.I))
    sections = list(dict.fromkeys(re.findall(r'(?:附录\s*|Appendix\s+)([A-Z])(?![A-Za-z])', text, re.I)))
    if sections:
        selected = []
        for label in sections:
            heading = re.compile(r'^\s*(?:Appendix\s+)?' + re.escape(label) +
                r'(?:[ \t]+|[.:][ \t]*)(?:(?-i:[A-Z])[^\n]{2,90})?$', re.I | re.M)
            start = next((i for i, p in enumerate(document.get('pages', []))
                if heading.search(p.get('text', ''))), None)
            if start is not None:
                for p in document['pages'][start:start + 4]:
                    if p is not document['pages'][start] and re.search(
                            r'^\s*Appendix\s+[A-Z]\b', p.get('text', ''), re.I | re.M):
                        break
                    selected.append(p.get('page_number', p.get('page')))
            # Some PDFs omit appendix letters from extracted headings. Use an
            # explicitly retained locator, bound to this exact source hash.
            for locator in document.get('section_locators', []):
                if locator['label'].casefold() == label.casefold():
                    selected.extend(locator['pages'])
        return sorted(set(selected) & set(available))[:4]
    if requested:
        return sorted(set(requested) & set(available))[:4]
    patterns = []
    if re.search(r'专家|expert', text, re.I):
        patterns.append(r'^.{0,40}Expert (?:Branch|Module|Network).{0,100}$')
    if re.search(r'递归|recursive|recursion|推断|inference', text, re.I):
        patterns.append(r'^.{0,40}(?:Recurs\w*|Inference).{0,40}$')
    if patterns:
        scored = [(sum(len(re.findall(pattern, p.get('text', ''), re.I | re.M)) for pattern in patterns),
                   p.get('page_number', p.get('page'))) for p in document.get('pages', [])]
        hits = [page for score, page in sorted(scored, key=lambda row: (-row[0], row[1])) if score]
        if hits:
            # The neighboring page often contains the rest of a split equation.
            selected = list(dict.fromkeys([page for hit in hits[:2] for page in (hit, hit + 1)]))
            return sorted(set(selected) & set(available))[:4]
    return available[:4]


def _specs(result, gate):
    from .literature_reading import _verified
    papers = {p['id']: p for p in result.get('papers', []) if _verified(p)}
    related = {p['paper_id']: p for p in result.get('analysis', {}).get('related_papers', [])}
    noted = {n['paper_id'] for n in result.get('notes', [])}
    current_requests = [q for vote in gate.get('votes', []) for q in vote.get('required_changes', [])]
    requests = [*current_requests, *result.get('analysis', {}).get('gaps', [])]
    labels = {pid: p['title'].split(':', 1)[0].strip() for pid, p in papers.items()}
    identifiers = {pid: [pid, p['title']] + ([labels[pid]] if 3 <= len(labels[pid]) <= 40
        and sum(label.casefold() == labels[pid].casefold() for label in labels.values()) == 1 else [])
        for pid, p in papers.items()}
    # One specifically named missing neighbor, followed by the already-known
    # closest work. Ordinary related RAG hits cannot substitute for that identity.
    missing = [pid for q in requests for pid, p in papers.items()
               if not re.search(r'留作后续|后续定向线索|本轮不扩展|暂不(?:扩展|阅读)', q)
               and (pid not in related or pid not in noted) and
               any(token.casefold() in q.casefold() for token in identifiers[pid])]
    named = list(dict.fromkeys(pid for q in current_requests for pid in papers
        if any(token.casefold() in q.casefold() for token in identifiers[pid])))
    ordered = list(dict.fromkeys([*list(dict.fromkeys(missing))[:1], *named]))
    ordered.extend(pid for vote in gate.get('votes', []) for work in vote.get('closest_work', [])
                   if (pid := work.get('paper_id')) in related and pid not in ordered)
    ordered = list(dict.fromkeys(ordered))
    ordered.extend(pid for pid in related if pid not in ordered)
    specs = []
    for pid in ordered:
        if pid not in papers or pid in result.get('reading_gate', {}).get('skipped_core_ids', []):
            continue
        paper = papers[pid]
        title = paper['title']
        questions = [q for q in current_requests
                     if any(token.casefold() in q.casefold() for token in identifiers[pid])]
        # Stale required_check page numbers must not override the current review.
        if not questions:
            questions = [related.get(pid, {}).get('required_check', '')]
        if pid not in related:
            questions.extend(q for q in requests if any(token.casefold() in q.casefold() for token in identifiers[pid]))
        for vote in gate.get('votes', []):
            questions.extend(q for q in vote.get('required_changes', [])
                             if any(token.casefold() in q.casefold() for token in identifiers[pid]))
        questions = list(dict.fromkeys(q for q in questions if q.strip()))[:4]
        if not questions:
            continue
        specs.append({'paper_id': pid, 'title': title, 'questions': questions,
                      'current_request': pid in named or pid in missing[:1]})
    return specs


async def adopt_section_sources(engine, harness, run_id, result, gate):
    """Verify retained alternate versions once; retrieval itself earns no reading."""
    specs = {s['paper_id']: s for s in _specs(result, gate)}
    for candidate in engine.store.list('stage_contexts', run_id):
        if candidate.get('kind') != 'novelty_alternate_source_candidate' or candidate.get('paper_id') not in specs:
            continue
        spec = specs[candidate['paper_id']]
        if not any(re.search(r'附录|Appendix', q, re.I) for q in spec['questions']):
            continue
        key = candidate['id'] + ':link-v1'
        previous = engine.store.get('stage_contexts', key)
        if previous:
            if (previous.get('status') != 'rejected' or previous.get('error') !=
                    'Alternate source identifier does not match the verified paper title/authors or DOI'):
                continue
            # Preserve the rejection. A stronger formal-registry check can
            # resolve shortened metadata once, never accept a prefix alone.
            key = candidate['id'] + ':link-v2'
            if engine.store.get('stage_contexts', key):
                continue
        record = {'id': key, 'run_id': run_id, 'kind': 'novelty_alternate_source_link',
                  'candidate_id': candidate['id'], 'created_at': now()}
        try:
            linked = await harness.execute_controller_tool(run_id, 'scout', 'literature.link_source',
                {k: candidate[k] for k in ('paper_id', 'source_id')}, purpose=f'novelty-source-link:{key}')
            record.update(status='completed', result=linked, completed_at=now())
        except (OSError, ValueError) as exc:
            from .storage import redact
            record.update(status='rejected', error=redact(str(exc))[:1000])
        engine.store.put('stage_contexts', key, record)


def _section_locators(engine, run_id, source_id, document):
    """Locator clues are source-bound navigation, never accepted excerpt evidence."""
    locators = []
    for candidate in engine.store.list('stage_contexts', run_id):
        if (candidate.get('kind') != 'novelty_alternate_source_candidate'
                or candidate.get('source_id') != source_id
                or candidate.get('text_sha256') != document['text_sha256']):
            continue
        for locator in candidate.get('section_locators', []):
            pages = [p for p in document.get('pages', []) if p.get('page_number', p.get('page')) in locator['pages']]
            if (pages and len(pages) == len(locator['pages']) and locator.get('basis_url')
                    and any(locator['heading'].casefold() in p['text'].casefold() for p in pages)):
                locators.append(locator)
    return locators


async def _acquire_missing(engine, harness, run_id, result, gate, progress_key):
    """RAG adoption precedes lawful acquisition; saved failures never loop."""
    from .literature_reading import has_fulltext
    key = progress_key + ':acquisition-v2'
    saved = engine.store.get('stage_contexts', key)
    if saved and saved['status'] == 'completed':
        # An older selector could spend its slot on a later full-title clue,
        # overlooking a current core paper named only by its unique acronym.
        # Retain that acquisition, allow one separately journaled correction.
        attempted = {p['paper_id'] for p in saved['papers']}
        pending_core = set(result.get('reading_gate', {}).get('pending_core_ids', [])) - attempted
        correction = next((spec for spec in _specs(result, gate) if spec['paper_id'] in pending_core), None)
        if not correction:
            return
        key = progress_key + ':acquisition-selection-v1'
        saved = engine.store.get('stage_contexts', key)
        if saved and saved['status'] == 'completed':
            return
        if not saved:
            saved = {'id': key, 'run_id': run_id, 'kind': 'novelty_acquisition', 'status': 'pending',
                     'papers': [correction], 'results': [], 'created_at': now(),
                     'previous_acquisition_id': progress_key + ':acquisition-v2'}
            engine.store.put('stage_contexts', key, saved)
    if not saved:
        pending = []
        for spec in _specs(result, gate):
            reading = engine.store.get('run_evidence', f'{run_id}:{spec["paper_id"]}', {}).get('reading', {})
            if reading.get('source_unavailable_code') == 'legal_fulltext_unavailable':
                # A new bibliographically verified alternate can be linked above.
                # Repeating the same exhausted publisher/OA lookup per evidence
                # round does not supply a missing mechanism or new read credit.
                continue
            if reading.get('source_id') and reading.get('retrieval_status') in {'pdf_text_indexed', 'web_text_indexed'}:
                try:
                    if has_fulltext(engine.sources.document(run_id, reading['source_id'])):
                        continue
                except (OSError, ValueError):
                    continue  # Corruption must not be replaced by a different document.
            pending.append(spec)
            break
        saved = {'id': key, 'run_id': run_id, 'kind': 'novelty_acquisition', 'status': 'pending',
                 'papers': pending, 'results': [], 'created_at': now()}
        engine.store.put('stage_contexts', key, saved)
    for spec in saved['papers'][len(saved['results']):]:
        # These papers already have verified discovery receipts in this task.
        # papers.download reuses the exact RAG identity before any network access.
        try:
            rows = await harness.execute_controller_tool(run_id, 'scout', 'papers.download',
                {'paper_ids': [spec['paper_id']], 'require_fulltext': True}, purpose=f'novelty-fulltext:{key}')
        except (OSError, ValueError) as exc:
            from .storage import redact
            rows = [{'retrieval_status': 'acquisition_rejected', 'error': redact(str(exc))[:600]}]
        saved['results'].append({'paper_id': spec['paper_id'], **{k: rows[0].get(k) for k in
            ('source_id', 'retrieval_status', 'provenance', 'rag_version_id', 'error', 'error_type', 'http_status')}})
        engine.store.put('stage_contexts', key, saved)
    saved.update(status='completed', completed_at=now())
    engine.store.put('stage_contexts', key, saved)


def local_targets(engine, run_id, result, gate, *, limit=2):
    """Only verified, scoped task papers with intact retained source text."""
    from .pdf_tools import LocalPDF
    targets = []
    for spec in _specs(result, gate):
        reading = engine.store.get('run_evidence', f'{run_id}:{spec["paper_id"]}', {}).get('reading', {})
        if not reading.get('source_id') or not reading.get('identity_verified'):
            continue
        try:
            document = engine.sources.document(run_id, reading['source_id'])
            if document.get('format') == 'pdf':
                LocalPDF(engine).original(run_id, reading['source_id'])
        except (OSError, ValueError):
            continue
        questions = spec['questions']
        pages = _pages(questions, {**document, 'section_locators':
            _section_locators(engine, run_id, reading['source_id'], document)})
        if document.get('format') == 'pdf' and not pages:
            continue  # An unavailable requested page cannot be replaced by an abstract.
        source_id = reading['source_id']
        calls = ([{'name': 'pdf.read', 'arguments': {'source_id': source_id, 'page': page,
                  'expected_sha256': document['text_sha256'], 'limit': 8000}} for page in pages]
                 if document.get('format') == 'pdf' else
                 [{'name': 'sources.read', 'arguments': {'source_id': source_id,
                   'expected_sha256': document['text_sha256'], 'limit': 8000}}])
        targets.append({**spec, 'source_id': source_id, 'text_sha256': document['text_sha256'],
                        'format': document.get('format'), 'read': calls})
        if limit is not None and len(targets) == limit:
            break
    return targets


def _already_covered(engine, run_id, target, before):
    from .innovation_evidence import added_keys
    document = engine.sources.document(run_id, target['source_id'])
    needed = []
    for call in target['read']:
        args = call['arguments']
        if call['name'] == 'pdf.read':
            page = next(p for p in document['pages'] if p.get('page_number', p.get('page')) == args['page'])
            start = page['start'] + args.get('offset', 0)
            end = min(page['end'], start + args.get('limit', 8000))
        else:
            start = args.get('offset', 0)
            end = min(len(document['text']), start + args.get('limit', 8000))
        text = document['text'][start:end]
        left = len(text) - len(text.lstrip())
        right = len(text.rstrip())
        if left < right:
            needed.append((target['paper_id'], target['text_sha256'], start + left, start + right))
    return bool(needed) and not added_keys(list(map(tuple, before)), needed)


async def _save_missing_note(engine, harness, run_id, result, job, handoff_id, analysis):
    """One structured extraction from completed reads; no new search/read loop."""
    from .engine import digest, obj
    from .innovation_routing import assignment
    from .literature_notes import LiteratureNoteValidationError
    key = handoff_id + ':note-save-v1'
    if engine.store.get('stage_contexts', key):
        return
    progress = engine.store.get('handoff_progress', handoff_id, {})
    reads = [copy.deepcopy(row) for row in progress.get('tool_results', [])
             if row.get('call', {}).get('name') in {'pdf.read', 'sources.read'}
             and isinstance(row.get('result'), dict) and not row['result'].get('error')
             and row['result'].get('source_id') == job['source_id'] and row['result'].get('text', '').strip()]
    if not reads or any(row.get('call', {}).get('name') == 'literature.record_notes'
        and isinstance(row.get('result'), dict) and row['result'].get('recorded_count', 0) > 0
        for row in progress.get('tool_results', [])):
        return  # No reading, or an already accepted note, must never buy another extraction.
    short = {'type': 'string', 'minLength': 1, 'maxLength': 1200}
    strings = {'type': 'array', 'maxItems': 8, 'items': short}
    citation = ({'citation_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}}
                if job['format'] == 'pdf' else {'locator': short, 'quote': {'type': 'string', 'minLength': 1, 'maxLength': 3200}})
    schema = obj({**{k: short for k in ('summary', 'research_question', 'method', 'relevance')},
        **{k: strings for k in ('datasets', 'baselines', 'limitations', 'missing_information')},
        'excerpts': {'type': 'array', 'minItems': 1, 'maxItems': 12, 'items': obj(citation)},
        'results': {'type': 'array', 'maxItems': 8, 'items': obj({
            **{k: short for k in ('dataset', 'metric', 'value', 'setting')}, **citation})}})
    paper = next(p for p in result['papers'] if p['id'] == job['paper_id'])
    context = {'paper': copy.deepcopy(paper), 'completed_read_receipts': reads,
        'analysis_already_completed': analysis, 'targeted_novelty_questions': job['questions'],
        'scope': 'Only these completed task-owned reads may be cited. Bibliographic verification is separate from source/scientific verification.'}
    instruction = ('把已经完成的阅读和分析整理为一篇可保存的中文文献笔记。只返回所需字段，不检索或重新阅读。'
        '只依据completed_read_receipts中的真实原文，分析摘要仅提供线索，未经这些片段支持的内容放入missing_information。'
        '选择决定性的摘录；PDF仅用所给citation_id，其他正文使用精确quote与locator。'
        '未读附录不妨碍保存已读范围的笔记，必须明确未读范围，不宣称全文已读或创新已批准。'
        '没有可对应引用的数值就results:[]，并说明缺失信息；value只放原文中的数值。'
        'paper_id/source_id/text_sha256由程序绑定，不自行返回。程序执行保存并校验回执。')
    purpose = 'literature_note_extract:novelty:' + digest([handoff_id, 1])
    instruction, context, schema = assignment(engine, run_id, purpose, instruction, context, schema)
    summary = analysis.get('summary', '')
    mentioned = set(re.findall(r'\bp[1-9][0-9]*:[0-9]+:[0-9]+:[0-9a-f]{8}\b', summary))
    available = {c['citation_id'] for row in reads for c in row['result'].get('citations', [])}
    selected = sorted(mentioned & available)
    if job['format'] == 'pdf' and selected and len(selected) <= 12 and summary and re.search(r'[\u3400-\u9fff]', summary):
        # The scout already authored this cited analysis. Preserve it verbatim;
        # do not buy another paraphrase merely to populate the note fields.
        issues = list(analysis.get('remaining_issues', []))
        extracted = {'summary': '复用已完成的片段阅读分析；原回答中的保存状态属于当时记录，本条笔记状态以保存回执为准。\n' + summary,
            'method': summary,
            'research_question': job['questions'][0],
            'relevance': '保存已完成的片段阅读分析，用于核查当前机制问题；科研结论未独立验证，创新与实验未审批。',
            'datasets': [], 'baselines': [], 'results': [], 'excerpts': [{'citation_id': cid} for cid in selected],
            'limitations': issues,
            'missing_information': [*issues, '数据集、基线及数值结果尚未转换为结构化字段，保留在原公开分析及对应摘录中；不宣称整篇论文已读完。']}
        derivation = 'verbatim_cited_scout_analysis'
    else:
        extracted = await harness.ask(run_id, 'scout', purpose, instruction, context, schema)
        derivation = 'one_structured_extraction_from_completed_reads'
    note = {**extracted, **{k: job[k] for k in ('paper_id', 'source_id', 'text_sha256')}}
    saved = {'id': key, 'run_id': run_id, 'kind': 'targeted_note_save', 'paper_id': job['paper_id'],
             'derivation': derivation, 'origin_analysis_handoff_id': handoff_id, 'origin_analysis_sha256': digest(analysis)}
    try:
        receipt = await harness.execute_controller_tool(run_id, 'scout', 'literature.record_notes',
            {'notes': [note]}, purpose=f'novelty-note-save:{key}')
        saved.update(status='completed', note_ids=[n['id'] for n in receipt['notes']], completed_at=now())
    except (LiteratureNoteValidationError, ValueError) as exc:
        from .storage import redact
        saved.update(status='validation_failed', error=redact(str(exc))[:1000],
                     validation=getattr(exc, 'details', None))
    engine.store.put('stage_contexts', key, saved)


async def complete(engine, harness, run_id, result, gate, progress_key, source_handoff_id, *, targets=None):
    """At most two frozen child jobs; failed/unchanged completions are reusable."""
    from .engine import Harness, digest, obj
    from .innovation_routing import assignment
    from .innovation_evidence import evidence_keys, added_keys
    key = progress_key + ':local-reading-v1'
    queue = engine.store.get('stage_contexts', key)
    await _acquire_missing(engine, harness, run_id, result, gate, progress_key)
    if not queue:
        targets = copy.deepcopy(targets) if targets is not None else local_targets(engine, run_id, result, gate)
        if not targets:
            return []
        before = evidence_keys(engine, run_id)
        queue = {'id': key, 'run_id': run_id, 'kind': 'novelty_local_reading', 'version': 1,
            'source_handoff_id': source_handoff_id, 'created_at': now(),
            'jobs': [{**target, 'before': [row for row in before if row[0] == target['paper_id']],
                      'status': 'reused' if _already_covered(engine, run_id, target, before) else 'pending',
                      'added_passages': 0} for target in targets]}
        engine.store.put('stage_contexts', key, queue)
        engine.store.event(run_id, 'innovation.reading_recovery',
            '已分派缺失原文片段的定向补读；保留旧笔记与付费回执。',
            {'paper_ids': [row['paper_id'] for row in targets], 'queue_id': key})
    else:
        # Upgrade a legacy abstract-only job without editing its frozen request.
        # Only these same two papers can earn one new original-body assignment.
        before = evidence_keys(engine, run_id)
        partial = {job['paper_id'] for job in queue['jobs'] if job['format'] == 'html'}
        for target in local_targets(engine, run_id, result, gate):
            if (target['paper_id'] not in partial or target['format'] != 'pdf'
                    or any(job['paper_id'] == target['paper_id'] and job['text_sha256'] == target['text_sha256']
                           for job in queue['jobs'])):
                continue
            queue['jobs'].append({**target, 'before': [row for row in before if row[0] == target['paper_id']],
                'status': 'reused' if _already_covered(engine, run_id, target, before) else 'pending', 'added_passages': 0})
            queue['version'] = 2
        engine.store.put('stage_contexts', key, queue)
    schema = obj({'summary': {'type': 'string', 'maxLength': 2000},
        'remaining_issues': {'type': 'array', 'maxItems': 8,
                             'items': {'type': 'string', 'maxLength': 1200}}})
    for index, job in enumerate(queue['jobs']):
        if job['status'] != 'pending':
            if job['status'] == 'no_new_evidence' and job.get('handoff_id'):
                await _save_missing_note(engine, harness, run_id, result, job, job['handoff_id'], job.get('result', {}))
                current = [row for row in evidence_keys(engine, run_id) if row[0] == job['paper_id']]
                added = added_keys(list(map(tuple, job['before'])), current)
                if added:
                    job.update(status='completed', added_passages=len(added), completed_at=now())
                    engine.store.put('stage_contexts', key, queue)
            continue
        focus = {k: copy.deepcopy(job[k]) for k in ('paper_id', 'source_id', 'text_sha256', 'read')}
        context = {'literature_notes_protocol': 1, 'pdf_tools_protocol': 1,
            **({'pdf_recovery_protocol': 2, 'pdf_reading_focus': focus} if job['format'] == 'pdf' else
               {'source_reading_focus': focus}),
            'targeted_novelty_questions': job['questions'],
            'paper': copy.deepcopy(next(p for p in result['papers'] if p['id'] == job['paper_id'])),
            'source_handoff_id': source_handoff_id,
            'scope': 'Historical notes are retained. Read only the missing mechanism passages in this task-owned source.'}
        reading_rule = (
            'tool_results中的pdf.read是真实本地PDF页片段。每条excerpts/results只用pdf.read返回的citation_id，'
            '程序带入精确原文；缺少决定性附录时用pdf.search定位后按需pdf.read。' if job['format'] == 'pdf' else
            'tool_results中的sources.read是保留正文的真实片段。保存笔记时使用已读片段的精确quote和locator。'
            '需要补充时只读取source_reading_focus.source_id，不引用目录或跳转页。')
        instruction = ('针对targeted_novelty_questions补查当前这一篇论文。' + reading_rule +
            '先分析这些片段，用literature.record_notes保存一篇补充笔记；必须检查保存成功回执。'
            '不例行重读完整论文。'
            '历史笔记已存在仍须补查当前未覆盖问题；不可只重述缺口或把目录当正文。'
            '明确已查看与未查看范围，保留反证；没有结果数值时results:[]并写missing_information。'
            '完成保存后返回中文summary和remaining_issues，不改写原综述、不审批创新或实验。')
        purpose = 'literature_reading:novelty-local:' + digest([progress_key, index])
        instruction, context, frozen_schema = assignment(engine, run_id, purpose, instruction, context, schema)
        handoff = harness._handoff(run_id, 'scout', purpose, instruction, context, frozen_schema)
        hid = handoff['id']
        progress = engine.store.get('handoff_progress', hid, {'seen': {}, 'turn': 0, 'tool_results': []})
        # Never append newly seeded pages to a frozen dispatched request. A crash
        # within the local batch resumes its cursor before any model dispatch.
        if not (progress.get('normal_request') or progress.get('finalization') or engine.store.get('decisions', hid)):
            if progress.get('pending_tool_calls'):
                await harness._drain_tools(run_id, 'scout', hid, progress)
            calls = ([{'name': 'pdf.info', 'arguments': {'source_id': job['source_id']}},
                      {'name': 'pdf.search', 'arguments': {'source_id': job['source_id'], 'query': 'Appendix', 'limit': 3}}]
                     if job['format'] == 'pdf' else []) + job['read']
            seen = {digest(row['call']) for row in progress['tool_results']}
            missing = [call for call in calls if digest(call) not in seen]
            if missing:
                # This batch is planned by the host before any model dispatch.
                # Freeze its exact calls as a separate current-turn contract;
                # an interrupted cursor continues under this same packet.
                frozen = engine.store.get('handoff_inputs', hid)
                version = frozen.get('tool_contract_version', 0)
                prepared = harness.prepare('scout', purpose, frozen['instruction'],
                    frozen['context'], frozen['schema'], progress['tool_results'],
                    tool_contract_version=version)
                item = {'enum': copy.deepcopy(missing)}
                if version == 1:
                    from .tool_contracts import call_schema
                    item = {'allOf': [call_schema([call['name'] for call in missing], prepared['context']), item]}
                prepared['schema'] = obj({'tool_calls': {'type': 'array',
                    'minItems': len(missing), 'maxItems': len(missing), 'items': item}})
                progress['tool_request'] = {'turn': progress['turn'], 'prepared': prepared,
                                            'prepared_sha256': digest(prepared)}
                progress.update(pending_tool_calls=missing, tool_cursor=0, tool_counted=False)
                harness._save_progress(run_id, hid, progress)
                await harness._drain_tools(run_id, 'scout', hid, progress)
        answer = await harness.ask(run_id, 'scout', purpose, instruction, context, frozen_schema)
        await _save_missing_note(engine, harness, run_id, result, job, hid, answer)
        current = {tuple(row) for row in evidence_keys(engine, run_id) if row[0] == job['paper_id']}
        added = added_keys(list(map(tuple, job['before'])), sorted(current))
        job.update(status='completed' if added else 'no_new_evidence', handoff_id=hid,
                   added_passages=len(added), result=answer, completed_at=now())
        engine.store.put('stage_contexts', key, queue)
    return [{k: copy.deepcopy(job.get(k)) for k in ('paper_id', 'title', 'source_id', 'text_sha256',
        'status', 'added_passages', 'handoff_id', 'result')} for job in queue['jobs']]
