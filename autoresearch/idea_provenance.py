"""Versioned proposal provenance; retrieved papers never prove project success.

Old proposal schemas remain untouched. Search evidence comes from hash-checked
task checkpoints, not a model's assertion that it searched the literature.
"""
from __future__ import annotations
from .prose_schema import validator as response_validator

import copy
import hashlib
import json
import re
from datetime import date

from jsonschema import Draft202012Validator

from .literature_reading import _run_papers
from .lab import _search_relevance


VERSION = 1
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1800, 'pattern': r'\S'}
ID = {'type': 'string', 'minLength': 1, 'maxLength': 500, 'pattern': r'\S'}


def _object(fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


def _array(item, maximum=12, minimum=0):
    return {'type': 'array', 'items': item, 'minItems': minimum, 'maxItems': maximum}


FIELDS = {
    'idea_rationale': _object({**{key: TEXT for key in ('origin', 'motivation', 'gap', 'mechanism', 'differentiation')},
        'problem_validation': _object({**{key: TEXT for key in ('target_scenario', 'affected_users',
            'observable_failure', 'why_it_matters', 'existing_solutions', 'why_insufficient',
            'necessity_check', 'abandonment_rule')}, 'evidence_ids': _array(ID, 12)}),
        'evidence': _array(_object({'paper_id': ID, 'claim': TEXT,
            'relation': {'enum': ['supports', 'contradicts', 'context']},
            'evidence_level': {'enum': ['metadata', 'abstract', 'full_text']},
            'locator': TEXT, 'limitation': TEXT}), 12)}),
    'paper_story': _object({**{key: TEXT for key in ('problem', 'gap', 'hypothesis', 'method')},
        'claims': _array(_object({'claim': TEXT, 'evidence_ids': _array(ID, 12), 'validation_plan': TEXT,
                                 'status': {'enum': ['hypothesis', 'supported', 'refuted']}}), 8, 1),
        'outline': _array(TEXT, 12, 1), 'title_candidates': _array(TEXT, 3, 1)}),
    'novelty_review': _object({'search_queries': _array(TEXT, 12, 1),
        'search_cutoff': {'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$'},
        'closest_work': _array(_object({'paper_id': ID, 'overlap': TEXT, 'differences': TEXT,
            'risk': {'enum': ['duplicate', 'high', 'medium', 'low']}, 'why_distinct': TEXT, 'required_check': TEXT}), 12),
        'conclusion': {'enum': ['duplicate_found', 'high_overlap', 'differentiated', 'insufficient_evidence']},
        'limitations': _array(TEXT, 12, 1)})}

PROPOSAL_INSTRUCTION = (
    '\n用简体中文完整填写 idea_rationale、paper_story 和 novelty_review。说明想法源于哪些问题和论文、'
    '为何提出、缺口、机制及与已有工作的实质区别；逐条引用本任务已核验论文的精确 paper_id，标明支持、'
    '反证或背景及证据层级与局限。metadata 仅指身份信息；abstract 必须有保存的摘要；full_text 必须关联'
    'literature.read_papers 的已保存阅读获取记录，locator 格式为 source_id#page=正整数 或 '
    'source_id#chars=起点:终点（字符位置从0开始），且定位到已保留文本。获取/索引不证明AI已阅读全文或科学主张成立。'
    'paper_story 解释问题→缺口→假设→方法→计划验证的主张→论文大纲及候选题名；本项目尚未实验的主张必须'
    '标为 hypothesis，不能用论文或叙事替代本项目结果。novelty_review 列出最相近工作、重合点、区别、风险及'
    '仍需检查内容。search_queries 是检索意图，实际执行范围以提供的检索回执为准；search_cutoff 使用回执日期。'
    '组合创新与架构改进可构成新颖性，需说明对当前痛点的实质作用、相对完整近邻方案的增量及对照/消融计划；'
    '不要求从0到1、组件全部原创或新理论。不能保证全网没有相同想法，不能以标题/词面差异证明创新；'
    '已完成检索范围内未发现高度相似完整机制可选differentiated继续，覆盖与效果未知保留为局限；未完成检索时明确 insufficient_evidence，'
    '发现重复或高重合时必须修改科学方案。保留反证和局限，不为获批虚构差异。每次修订都返回这三个完整对象。')
PROPOSAL_INSTRUCTION += ('\nproblem_validation 必须解释真实目标场景、受影响的使用者、可观察失败、为何重要、'
    '已有解法及为何仍不足、新增机制如何带来实质效果及其资源需求，并给出放弃本想法的条件。引用本任务已核验且实际阅读过的论文ID；'
    '没有证据就明确缺失，不得编造现实需求。允许本机资源与预算内可运行的复杂机制，不以简单或轻量为前提；'
    '已有方法在同等条件下已满足目标且候选没有额外实质价值时，应承认并放弃，不能只因对照更简单就否决候选。')
PROPOSAL_INSTRUCTION += '结合近期强基线确认问题今天仍然存在，不把过时或已被解决的限制当作当前痛点。'

REVIEW_INSTRUCTION = (
    '\n审查 idea_rationale 的来源、提出理由、机制、实质差异和每篇佐证/反证的证据层级，及 paper_story '
    '是否把待验证假说误写成结果。审查 novelty_review 最相近工作的重合与区别，不能凭换措辞认定创新。'
    '接受针对痛点且有机制依据和验证计划的组合创新、架构改进；区分组件重合与整体贡献重合，'
    '不能因为已有组件、局部改动或缺少全新理论就判为小修小改。方法类型不限，以当前最相似工作为主要对照，'
    '检查痛点是否仍未解决及方案的实质增量；仅按具体归因需要增加简单基线或消融，不将它们设为统一前提。'
    '文献研究员须使用 literature.survey 反向检索至少两种机制同义表达，覆盖近期方法和强基线，并对最相近论文'
    '至少阅读已保存摘要或可定位全文片段；保存工具回执后独立评议。其他角色也须检查这些证据与资源、验证计划'
    '是否一致。有原文依据的完整机制duplicate_found、high_overlap或明确duplicate/high线索尚未解决时不得批准。'
    '已完成检索与核心阅读，且整体机制没有高度相似证据时，通过新颖性检查；'
    'insufficient_evidence、覆盖不完整或尚未验证能力不能单独当作不新颖，原意见与局限保留。'
    '检索不是穷尽证明，三票也不证明创新或实验效果。修订建议可用 novelty、references、hypothesis '
    '等现有 target_field 指明相关来源/叙事问题，不能把改写故事本身当作实质科研进展。')
REVIEW_INSTRUCTION += ('\n逐角色独立填写 problem_review：判断痛点有无证据(grounded)、新增方法是否必要(necessary)，'
    '引用已核验且已阅读的paperID，给出当前最相似工作的比较与可证伪条件。方法类型不限、机制无需简单，在本机真实资源与预算内可运行即可评审；'
    '应否定伪问题、已有方法同等条件下已满足目标且候选无实质增量、或增益缺乏实践意义的想法。'
    'CCF-A等投稿目标不能证明问题必要性，不能为了故事弱化强基线。')

PROBLEM_REVIEW = _object({'grounded': {'type': 'boolean'}, 'necessary': {'type': 'boolean'},
    'evidence_ids': _array(ID, 12), 'baseline_alternative': TEXT, 'falsification': TEXT, 'rationale': TEXT})


def proposal_schema(base, context):
    from .innovation_quality import proposal_schema as quality_schema
    base = quality_schema(base, context)
    if context.get('idea_provenance_protocol') != VERSION:
        return base
    schema = copy.deepcopy(base)
    schema['properties'].update(copy.deepcopy(FIELDS))
    schema['required'] = [*schema['required'], *FIELDS]
    if context.get('resource_plan_protocol') == 1:
        from .resource_contract import plan_schema
        schema['properties']['resource_plan'] = plan_schema()
    return schema


def review_schema(base, context):
    if context.get('idea_provenance_protocol') != VERSION:
        return base
    schema = copy.deepcopy(base)
    schema['properties']['problem_review'] = copy.deepcopy(PROBLEM_REVIEW)
    schema['required'] = [*schema['required'], 'problem_review']
    return schema


def assess_problem_reviews(store, run_id, votes, *, reading_note_ids=None):
    allowed = set(_run_papers(store, run_id))
    issues, roles = [], []
    if len(votes) != 3 or {vote.get('role') for vote in votes} != {'director', 'engineer', 'scout'}:
        issues.append('问题必要性评审需要三个不同角色的独立意见')
    for vote in votes:
        review = vote.get('problem_review')
        errors = list(response_validator(PROBLEM_REVIEW).iter_errors(review))
        failures = []
        if errors:
            failures.append('问题必要性评审结构缺失或不完整')
        else:
            if not review['grounded'] or not review['necessary']:
                failures.append('尚未确认真实痛点或新增方法的必要性')
            if not review['evidence_ids']:
                failures.append('痛点评审尚未引用本任务证据')
            if set(review['evidence_ids']) - allowed:
                failures.append('痛点评审引用了本任务之外或未核验的文献')
            if reading_note_ids is not None and set(review['evidence_ids']) - reading_note_ids:
                failures.append('痛点评审缺少对应的合格阅读笔记')
        roles.append({'role': vote.get('role'), 'approved': not failures, 'issues': failures})
        issues.extend(f'{vote.get("role")}: {message}' for message in failures)
    return {'approved': not issues, 'issues': issues, 'roles': roles,
            'scope': '三方公开判断及来源门禁，不证明真实需求、必要性或科研结果已经成立。'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def search_scope(store, run_id):
    """Exact executed scope, including bounded failures; no inferred exhaustiveness."""
    records = store.list('literature_inputs', run_id)
    plans = {r.get('plan_id'): r for r in records if r.get('kind') == 'survey_plan'
             and isinstance(r.get('query_plan'), dict) and r.get('plan_id') == _hash(r['query_plan'])}
    headers = [e['id'] for e in store.role_tool_events(run_id, 'scout')
               if e.get('kind') == 'tool.completed' and e.get('message') == 'literature.survey']
    scout_plans = {e.get('data', {}).get('result', {}).get('coverage', {}).get('plan_id')
                   for e in store.role_tool_events(run_id, 'scout', event_ids=headers)
                   if e.get('kind') == 'tool.completed' and e.get('message') == 'literature.survey'
                   and isinstance(e.get('data', {}).get('result'), dict)}
    queries = []
    for row in records:
        plan = plans.get(row.get('plan_id'))
        item = row.get('query_plan_item')
        if (row.get('kind') != 'survey_query' or not plan or row.get('status') != 'completed'
                or not isinstance(item, dict) or item not in plan['query_plan'].get('batches', [])
                or row.get('result_sha256') != _hash([row.get('papers'), row.get('diagnostics')])):
            continue
        diagnostic = row.get('diagnostics') or {}
        try:
            cutoff = date.fromisoformat(plan['query_plan']['as_of_date'])
        except (KeyError, TypeError, ValueError):
            continue
        successful = any(isinstance(s, dict) and s.get('status') in {'ok', 'success', 'completed'}
                         for s in diagnostic.get('sources', {}).values())
        queries.append({'receipt_id': row['id'], 'plan_id': row['plan_id'],
            'as_of_date': plan['query_plan']['as_of_date'],
            'query': item.get('query'), 'base_query': item.get('base_query'), 'facet': item.get('facet'),
            'sort': item.get('sort'), 'year_from': item.get('year_from'), 'year_to': item.get('year_to'),
            'scout_requested': row['plan_id'] in scout_plans, 'successful': successful,
            'errors': copy.deepcopy(diagnostic.get('errors', [])),
            'paper_ids': [p['id'] for p in row.get('papers', []) if p.get('verified') is True
                          and p.get('verification', {}).get('status') == 'verified'
                          and type(p.get('year')) is int and p['year'] <= cutoff.year
                          and _search_relevance(str(item.get('base_query', '')), p)['accepted']
                          and (item.get('sort') != 'recent' or
                               (type(item.get('year_from')) is int and type(item.get('year_to')) is int
                                and item['year_from'] <= p['year'] <= item['year_to']))]})
    return {'queries': sorted(queries, key=lambda r: r['receipt_id']), 'exhaustive': False,
            'policy': 'Actual scope comes only from saved query receipts. Model query descriptions do not establish execution.'}


def _full_text(store, run_id, paper_id, locator, document_loader):
    from .paper_bindings import readings_for, same_source
    match = re.fullmatch(r'(.+)#(?:page=([1-9]\d*)|chars=(\d+):(\d+))', locator)
    if not match or not document_loader:
        return False
    try:
        doc = document_loader(run_id, match[1])
    except (ValueError, OSError):
        return False
    if doc.get('run_id') != run_id:
        return False
    # Resolve all task source aliases of this canonical paper. Version hashes
    # and owned acquisition receipts still decide whether a locator is valid.
    readings = [reading for _, reading in readings_for(store, run_id, paper_id, allow_legacy_current=True)]
    bound = False
    for reading in readings:
        if (not reading.get('source_id')
                or reading.get('retrieval_status') not in {'pdf_text_indexed', 'web_text_indexed'}
                or not reading.get('text_sha256') or not reading.get('content_sha256')
                or doc.get('text_sha256') != reading['text_sha256']
                or doc.get('content_sha256') != reading['content_sha256']):
            continue
        if reading['source_id'] != match[1]:
            try:
                current = document_loader(run_id, reading['source_id'])
            except (ValueError, OSError):
                continue
            if (current.get('run_id') != run_id
                    or current.get('text_sha256') != reading['text_sha256']
                    or current.get('content_sha256') != reading['content_sha256']):
                continue
        bound = True
        break
    if not bound:
        return False
    if match[2]:
        return any(p.get('page_number', p.get('page')) == int(match[2]) and str(p.get('text', '')).strip()
                   for p in doc.get('pages', []))
    start, end = int(match[3]), int(match[4])
    return 0 <= start < end <= len(doc.get('text', '')) and bool(doc['text'][start:end].strip())


def assess(store, run_id, proposal, *, document_loader=None, reading_note_ids=None):
    """Assess source availability and novelty admission, never scientific truth."""
    issues = []
    candidate = {key: proposal.get(key) for key in FIELDS}
    errors = list(response_validator(_object(FIELDS)).iter_errors(candidate))
    if errors:
        return {'approved': False, 'issues': ['想法来源与论文叙事结构缺失或无效：' +
                    '/'.join(map(str, e.absolute_path)) for e in errors[:12]], 'protocol': VERSION}
    papers = _run_papers(store, run_id)
    from .paper_identity import task_paper
    # Identifier projection is admission-only; saved proposal/vote hashes and
    # their original locators are never rewritten.
    proposal = copy.deepcopy(proposal)
    def owned(identifier):
        paper = task_paper(store, run_id, identifier, papers)
        return paper['id'] if paper else identifier
    proposal['references'] = [owned(x) for x in proposal.get('references', [])]
    for name, key in (('idea_rationale', 'evidence'), ('novelty_review', 'closest_work')):
        for item in proposal[name][key]:
            item['paper_id'] = owned(item['paper_id'])
    for claim in proposal['paper_story']['claims']:
        claim['evidence_ids'] = [owned(x) for x in claim['evidence_ids']]
    validation = proposal['idea_rationale']['problem_validation']
    validation['evidence_ids'] = [owned(x) for x in validation['evidence_ids']]
    if reading_note_ids is not None:
        reading_note_ids = {owned(x) for x in reading_note_ids}
    idea, story, novelty = (proposal[key] for key in FIELDS)
    cited = set(proposal.get('references', [])) | {e['paper_id'] for e in idea['evidence']}
    cited.update(e['paper_id'] for e in novelty['closest_work'])
    cited.update(i for claim in story['claims'] for i in claim['evidence_ids'])
    problem_ids = set(idea['problem_validation']['evidence_ids'])
    cited.update(problem_ids)
    missing = sorted(cited - papers.keys())
    if missing:
        issues.append('引用不属于本任务已核验论文：' + ', '.join(missing))
    if not problem_ids:
        issues.append('问题痛点与必要性尚未引用本任务文献证据')
    if reading_note_ids is not None and problem_ids - reading_note_ids:
        issues.append('问题痛点与必要性缺少对应的合格阅读笔记')
    substantive = set()
    for item in idea['evidence']:
        paper = papers.get(item['paper_id'], {})
        level = item['evidence_level']
        available = level == 'metadata' and bool(paper)
        if level == 'abstract':
            available = isinstance(paper.get('abstract'), str) and bool(paper['abstract'].strip())
        elif level == 'full_text':
            available = bool(paper) and _full_text(store, run_id, item['paper_id'], item['locator'], document_loader)
            if reading_note_ids is not None:
                available = available and item['paper_id'] in reading_note_ids
        if not available:
            issues.append(f"{item['paper_id']} 的 {level} 证据没有匹配的任务原件或有效定位")
        elif level != 'metadata':
            substantive.add(item['paper_id'])
    if any(claim['status'] != 'hypothesis' for claim in story['claims']):
        issues.append('提案阶段的本项目论文主张必须标为 hypothesis；论文叙事不能代替本项目实验结果')
    from . import innovation_policy
    explicit_scope_policy = (store.get('runs', run_id, {}).get('config', {}).get('innovation_policy_version')
                             == innovation_policy.VERSION)
    scope_conclusion = explicit_scope_policy and novelty['conclusion'] in {'differentiated', 'insufficient_evidence'}
    if novelty['conclusion'] != 'differentiated' and not scope_conclusion:
        issues.append('近似工作审查尚未形成有证据支持的实质差异：' + novelty['conclusion'])
    if not novelty['closest_work']:
        issues.append('尚未列出最相近的已核验工作，不能据此认定不存在重复想法')
    for item in novelty['closest_work']:
        if item['risk'] in {'duplicate', 'high'}:
            issues.append(f"{item['paper_id']} 仍存在重复或高重合风险，须修改科学方案")
        if item['paper_id'] not in substantive:
            issues.append(f"{item['paper_id']} 的差异判断缺少摘要或可定位全文证据，元数据不足以证明创新")
        if reading_note_ids is not None and item['paper_id'] not in reading_note_ids:
            issues.append(f"{item['paper_id']} 的最相近工作比较缺少经实际原文摘录校验的阅读笔记")
    scope = search_scope(store, run_id)
    successful = [q for q in scope['queries'] if q['successful'] and q['scout_requested']]
    try:
        cutoff = date.fromisoformat(novelty['search_cutoff'])
        dates = {date.fromisoformat(q['as_of_date']) for q in successful}
    except ValueError:
        cutoff, dates = None, set()
    if cutoff not in dates:
        issues.append('检索截止日期没有匹配的文献研究员实际检索回执')
    selected = [q for q in successful if q['as_of_date'] == novelty['search_cutoff']]
    if (len({str(q['base_query']).strip().casefold() for q in selected}) < 2
            or not any(q['sort'] == 'recent' for q in selected)
            or not any(q['facet'] == 'foundational_baselines' for q in selected)):
        issues.append('尚缺文献研究员至少两种查询表达、近期工作和基线检索的真实回执；仍需反向检索与独立审查')
    retrieved = {p for q in selected for p in q['paper_ids']}
    if any(item['paper_id'] not in retrieved for item in novelty['closest_work']):
        issues.append('最相近工作未关联到所声明截止日期的实际检索范围')
    return {'approved': not issues, 'protocol': VERSION, 'issues': issues,
            'unverified_paper_ids': missing, 'search_scope': scope,
            'scientific_claims_verified': False, 'novelty_proven': False,
            **({'novelty_policy_version': innovation_policy.VERSION,
                'novelty_policy': '已检索范围内未发现高度相似完整机制即可继续；覆盖与能力局限仍保留。'}
               if explicit_scope_policy else {}),
            'scope': '仅核对任务来源、证据层级及近似工作评议前提；不证明穷尽检索、绝对创新或科研结果。'}
