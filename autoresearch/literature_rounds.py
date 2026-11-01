"""Separate recent idea discovery from proposal-specific prior-art analysis."""
from __future__ import annotations

import calendar
import copy
from datetime import date

from .storage import now


def enabled(run):
    return run.get('config', {}).get('literature_protocol') == 2


def shift_months(value, months):
    total = value.year * 12 + value.month - 1 + months
    year, month = total // 12, total % 12 + 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def policy(store, run_id):
    key = f'{run_id}:literature-date-policy'
    def freeze(old):
        if old:
            return old
        today = date.today()
        return {'id': key, 'run_id': run_id, 'kind': 'literature_date_policy', 'as_of_date': today.isoformat(),
            'date_from': shift_months(today, -12).isoformat(), 'preferred_from': shift_months(today, -6).isoformat(),
            'date_to': today.isoformat(), 'version': 2, 'created_at': now(),
            'scope': '第一轮优先近6个月、主体限定近12个月；第二轮按idea相关性检索，不限制发表年代。日期不明或未来日期不能充当近期依据。'}
    return store.mutate('literature_inputs', key, freeze)


def phase(run):
    return 'idea' if run.get('stage') in {'intake', 'literature', 'innovation'} else 'related_work'


def publication_date(paper):
    try:
        value = paper.get('publication_date')
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def recent(paper, window):
    published = publication_date(paper)
    return bool(published and window['date_from'] <= published.isoformat() <= window['date_to'])


def filter_papers(store, run_id, papers):
    run = store.get('runs', run_id)
    from . import venue_policy
    from .reference_papers import is_uploaded_paper
    uploaded = lambda paper: is_uploaded_paper(store, run_id, paper)
    if venue_policy.enabled(run):
        papers = [p for p in papers if uploaded(p) or venue_policy.assess(p)['eligible']]
    if not enabled(run) or phase(run) != 'idea':
        return papers
    window = policy(store, run_id)
    return sorted([p for p in papers if uploaded(p) or recent(p, window)],
                  key=lambda p: p.get('publication_date') or '', reverse=True)


def reading_key(store, run_id):
    from .innovation_review import suffix as attempt_suffix
    run = store.get('runs', run_id)
    suffix = f":related-work:{run.get('state', {}).get('research_cycle', 1)}" if enabled(run) and phase(run) == 'related_work' else ''
    if suffix:
        suffix += attempt_suffix(store, run_id)
    return f'{run_id}{suffix}:reading-analysis'


def persist_index(engine, intelligence, run_id, name, payload):
    """Keep content on disk and in searchable memory; hand off only this manifest."""
    import json
    from .engine import digest
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    artifact = engine._literature_artifact(run_id, f'literature/index/{name}-{digest(payload)[:16]}.json', content, 'literature_index')
    memory = intelligence.memory_add(run_id, 'shared', content,
        {'kind': 'literature_index', 'artifact_id': artifact['id'], 'shared': False, 'provenance': 'indexed_research_record'})
    return {'artifact_id': artifact['id'], 'sha256': artifact['sha256'], 'memory_document_id': memory['document_id'],
        'chunk_count': len(memory['chunk_ids']), 'name': name, 'provenance': 'indexed_research_record',
        'read': 'artifacts.read(id=artifact_id,offset=0,limit=2400); memory.search(query=...,limit=4)',
        'scope': '只传索引，原文与分析按需检索读取；该记录不证明科研结论。'}


def shared_packet(engine, run_id, output, intelligence):
    """Persist grounded content and give all three candidates the same index."""
    from .literature_notes import notes_view
    core = set(output.get('reading_gate', {}).get('noted_core_ids', []))
    papers = {p['id']: p for p in output.get('papers', [])}
    notes = notes_view(engine.store, run_id, document_loader=engine.sources.document)
    selected = {}
    for note in notes:
        if note['paper_id'] in core:
            selected[note['paper_id']] = note
    from .literature_reading import uploaded_rag_evidence
    external_reuses = uploaded_rag_evidence(engine, run_id)
    for paper_id, evidence in external_reuses.items():
        payload = evidence['payload']
        if paper_id not in selected and payload.get('provenance') == 'external_agent_analysis':
            selected[paper_id] = {**copy.deepcopy(payload['note']), 'paper_id': paper_id,
                'id': payload['origin_note_id'], 'source_id': evidence['source_id'], 'generation': 'reused_external_analysis',
                'provenance': 'external_agent_analysis',
                'reused_from': {'rag_entry_id': engine.paper_rag.canonical_id(payload['entry_id']),
                    'rag_version_id': evidence['rag_version_id'], 'run_id': payload['origin_run_id'],
                    'note_id': payload['origin_note_id'], 'analysis_created_at': payload['analysis_created_at']},
                'scope_note': '复用外部历史分析，不授予本任务原文阅读信用；科研主张仍未核验。'}
            for excerpt in selected[paper_id].get('excerpts', []):
                excerpt['locator'] = excerpt['locator'].replace(payload['note']['source_id'] + '#',
                    evidence['source_id'] + '#', 1)
    rows = []
    for paper_id, note in sorted(selected.items()):
        paper = papers.get(paper_id, {})
        rows.append({'paper_id': paper_id, 'title': paper.get('title'), 'publication_date': paper.get('publication_date'),
            'note_id': note['id'], 'source_id': note['source_id'], 'text_sha256': note['text_sha256'],
            **{key: copy.deepcopy(note[key]) for key in ('summary', 'research_question', 'method', 'datasets',
                'baselines', 'limitations', 'relevance', 'missing_information')},
            'architecture_and_method': note['method'],
            'excerpts': [{**e, 'quote': e['quote'][:1200], 'display_truncated': len(e['quote']) > 1200}
                         for e in note['excerpts'][:2]],
            'reading_scope': 'inspected_passages', 'scientific_claims_verified': False})
        if note.get('reused_from'):
            rows[-1].update(reused_from=note['reused_from'], generation=note['generation'],
                            scope_note=note['scope_note'])
        if paper.get('identity_kind') == 'uploaded_document':
            rows[-1].update(identity_kind='uploaded_document', source_reference_id=paper.get('source_reference_id'),
                content_integrity_verified=True,
                bibliographic_identity_verified=paper.get('bibliographic_identity_verified') is True,
                evidence_role='uploaded_seed')
        if note.get('provenance') == 'external_agent_analysis':
            rows[-1].update(provenance='external_agent_analysis', primary_source_reading=False,
                            new_reading_credit=False)
    payload = {'round': 'idea', 'date_policy': policy(engine.store, run_id), 'summary': output.get('analysis', {}).get('summary'),
        'papers': rows, 'audience': ['director', 'engineer', 'scout'],
        'scope': ('同一份已核验片段笔记和用户选定上传seed供三方提出创新点；'
                  '明确归属的外部RAG分析保留未核验状态，不计本任务新阅读。架构不明时保留缺失，不把索引或摘要当作全文阅读。')}
    return {**persist_index(engine, intelligence, run_id, 'idea-reading', payload),
            'round': 'idea', 'paper_count': len(rows), 'as_of_date': payload['date_policy']['as_of_date'],
            'audience': payload['audience']}


IDEA_INSTRUCTION = ('\n这是第一轮：近期论文分析，用于发现idea，不是经典基线综述。'
    '按literature_date_policy截至日，优先近6个月，主体只用近12个月且有明确发表日期的论文。'
    'DLinear、PatchTST等旧方法只能作为近期论文中必要的历史背景，不得把它们列为本轮核心或凑近期数量。'
    '阅读论文的研究问题、架构组成、输入输出、模块连接、训练/推理流程、实验与局限。'
    'literature.record_notes的method必须描述上述架构信息；未在已查看原文找到的内容写入missing_information，不得推断补造。'
    'summary总结近期进展、尚未解决的问题和可探索方向。正文笔记、方法架构和局限保存在持久记忆中；系统仅同步同一索引给三个agent，随后各自按需读取并提出idea。'
    '用户创建任务时明确上传的seed论文独立保留，不受年代或出处筛选限制；逐篇精读并保存笔记，'
    '已有RAG分析则按原来源和日期复用，不重复分析。把上传seed与本轮最新工作一起作为idea依据，'
    '不得将旧seed或日期不明seed列为近期进展。只剩旧论文或日期不明时报告近期工作缺口，不能偷偷扩大到多年以前。')

RELATED_INSTRUCTION = ('\n这是第二轮：选定idea之后的最相关论文分析。围绕selected_idea的机制、结构、问题与同义表述反向检索。'
    '本轮不设发表年代门槛：需要同时找到最相似方法、经典源头、强基线、直接竞争工作和反例，DLinear/PatchTST等在确实相关时应纳入。'
    '按实质相关性而非年份排序；related_papers逐篇说明role、relevance_to_idea、overlap、difference、required_check。'
    '所有related_papers都必须列入core_paper_ids；可访问的论文完成实际阅读笔记，不能只用第一轮的近期清单替代针对idea的新检索。'
    '若literature_access_policy允许跳过且系统已记录来源不可用，则保留该论文和失败原因，明确内容未核验；'
    '不为它补造笔记、不反复下载、不因单篇访问失败停止处理其他论文。重合点和差异只写实际已读证据，未读论文标为未知。'
    '不能声称穷尽或仅凭命名差异证明创新；把重复风险和不确定性原样交给接下来的三方共识。')
