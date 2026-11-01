"""Deterministic Chinese idea dossier: source traceability, prior art and paper argument.

This renderer preserves recorded claims and limitations. It never confers scientific
approval, invents citations, or treats a literature identity check as claim validation.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def _text(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return '未记录'
    return str(value).replace('<', '&lt;').replace('>', '&gt;').strip() or '未记录'


def _items(value):
    return value if isinstance(value, list) else []


def _object(value):
    return value if isinstance(value, dict) else {}


def render_idea_materials(proposal, papers=None, *, assessment=None, cycle=None, round_number=None):
    """Render the exact saved proposal, including rejected or unverified alternatives."""
    proposal = _object(proposal)
    indexed = {p['id']: p for p in _items(papers) if isinstance(p, dict) and isinstance(p.get('id'), str)}
    lines = ['# Idea 出处、创新性核查与论文论证材料', '', f'## {_text(proposal.get("title"))}', '',
             '本材料由已保存的提案确定性整理，不新增模型推理或实验结论。提案中的判断仍需独立评议与实验核验。',
             '有限检索不能保证全网不存在相同或高度相似的 idea；文献身份核验不等于其结论或本方案创新性已被证明。']
    if cycle is not None or round_number is not None:
        lines.extend(['', f'研究周期：{_text(cycle)}；讨论轮次：{_text(round_number)}。'])

    def paragraph(label, value):
        lines.extend(['', f'### {label}', '', _text(value)])

    def bullets(label, values):
        lines.extend(['', f'### {label}', ''])
        lines.extend('- ' + _text(value) for value in _items(values))
        if not _items(values):
            lines.append('未记录')

    def source(identifier):
        key = str(identifier) if isinstance(identifier, str) else ''
        paper = indexed.get(key)
        lines.append(f'- 来源标识：{_text(key)}')
        if not paper:
            lines.append('- 未找到该任务中的对应来源元数据，不补造标题或链接。')
            return
        lines.append(f'- 文献：{_text(paper.get("title"))}')
        lines.append('- 身份核验：' + ('标识与元数据已核验；不代表主张已验证。' if paper.get('verified') is True else '尚未核验。'))
        url = paper.get('url')
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            if parsed and parsed.scheme in ('http', 'https') and parsed.netloc and not parsed.username and not parsed.password:
                lines.append('- 来源链接：' + _text(url))
        except ValueError:
            pass

    rationale = _object(proposal.get('idea_rationale'))
    lines.extend(['', '## 一、为什么提出这个 idea'])
    if not rationale:
        lines.extend(['', '历史提案未记录结构化的 idea 出处与论证，不能从结果反推补造。'])
    for key, label in [('origin', '想法来源'), ('motivation', '研究动机'), ('gap', '现有工作缺口'),
                       ('mechanism', '预期作用机制'), ('differentiation', '与已有方法的区别')]:
        if rationale:
            paragraph(label, rationale.get(key))
    pain = _object(rationale.get('problem_validation'))
    lines.extend(['', '## 痛点是否真实、改进是否必要', '',
                  '先核对可观察的问题和现有解法，再组织论文论证。发表目标、故事完整或模型投票本身均不是痛点真实的证据。'])
    if not pain:
        lines.extend(['', '此版本未记录结构化痛点验证；不根据投稿目标补造需求。'])
    for key, label in [('target_scenario', '具体场景'), ('affected_users', '受影响的使用者或研究对象'),
                       ('observable_failure', '可观察的失败现象'), ('why_it_matters', '问题为何重要'),
                       ('existing_solutions', '已有解决办法'), ('why_insufficient', '已有办法为何不足'),
                       ('necessity_check', '新方法是否必要的检验'), ('abandonment_rule', '什么结果出现就应放弃或缩小想法')]:
        if pain:
            paragraph(label, pain.get(key))
    if pain:
        lines.extend(['', '### 痛点依据', ''])
        for identifier in _items(pain.get('evidence_ids')):
            source(identifier)
    lines.extend(['', '## 二、佐证论文与反面证据'])
    relations = {'supports': '支持', 'contradicts': '反证或冲突', 'context': '研究背景'}
    levels = {'metadata': '仅元数据', 'abstract': '摘要', 'full_text': '全文内容'}
    for position, entry in enumerate(_items(rationale.get('evidence')), 1):
        item = _object(entry)
        lines.extend(['', f'### 证据 {position}', ''])
        source(item.get('paper_id'))
        lines.extend([f'- 与主张的关系（提案记录）：{relations.get(item.get("relation"), "未记录")}',
                      f'- 读取层级（须对照读取回执）：{levels.get(item.get("evidence_level"), "未记录")}',
                      f'- 原文定位：{_text(item.get("locator"))}',
                      f'- 支持或反对的具体论点：{_text(item.get("claim"))}',
                      f'- 证据的局限：{_text(item.get("limitation"))}'])
    if not _items(rationale.get('evidence')):
        lines.extend(['', '未记录逐项证据。'])

    novelty = _object(proposal.get('novelty_review'))
    lines.extend(['', '## 三、重复与高相似工作的核查'])
    conclusions = {'duplicate_found': '发现相同工作，需要放弃或实质修改', 'high_overlap': '高度相似，需要实质修改',
                   'differentiated': '提案认为存在可检验的差异，仍需独立复核', 'insufficient_evidence': '检索证据不足，待核验'}
    paragraph('当前判断', conclusions.get(novelty.get('conclusion'), '尚未记录创新性核查'))
    paragraph('提案声明的检索截止日期（须对照检索回执）', novelty.get('search_cutoff'))
    bullets('检索词（须对照实际检索记录）', novelty.get('search_queries'))
    risks = {'duplicate': '相同', 'high': '高度相似', 'medium': '中等相似', 'low': '较低相似'}
    for position, entry in enumerate(_items(novelty.get('closest_work')), 1):
        item = _object(entry)
        lines.extend(['', f'### 最接近工作 {position}', ''])
        source(item.get('paper_id'))
        lines.extend([f'- 相似风险（提案记录）：{risks.get(item.get("risk"), "未记录")}',
                      f'- 重叠之处：{_text(item.get("overlap"))}', f'- 实质差异：{_text(item.get("differences"))}',
                      f'- 为什么并非相同方法：{_text(item.get("why_distinct"))}',
                      f'- 尚需核对或实验：{_text(item.get("required_check"))}'])
    bullets('检索范围与创新判断的局限', novelty.get('limitations'))
    if assessment is not None:
        gate = _object(assessment)
        provenance_gate = _object(gate.get('idea_provenance'))
        paragraph('程序证据检查', '通过引用与检索记录检查，不代表创新已获证明。' if gate.get('approved') is True
                  else '尚未通过证据检查；不得把提案叙述当作立项批准。')
        # Reason records are retained separately in the full research archive.
        for reason in _items(gate.get('issues')) + _items(gate.get('reasons')) + _items(provenance_gate.get('issues')):
            lines.append('- ' + _text(reason))
        scope = _object(provenance_gate.get('search_scope') or gate.get('search_scope'))
        if _items(scope.get('queries')):
            lines.extend(['', '### 实际检索回执范围', '', '以下来自任务保存的回执，与提案中声明的检索词分开记录。'])
            for query in scope['queries']:
                item = _object(query)
                lines.append(f'- {_text(item.get("query"))}；日期：{_text(item.get("as_of_date"))}；'
                             f'回执：{_text(item.get("receipt_id"))}；'
                             f'检索成功：{"是" if item.get("successful") is True else "否或未确认"}')

    story = _object(proposal.get('paper_story'))
    lines.extend(['', '## 四、论文故事与验证路线'])
    for key, label in [('problem', '实际问题'), ('gap', '前人工作的不足'), ('hypothesis', '可证伪假设'), ('method', '方法与机制')]:
        paragraph(label, story.get(key))
    status = {'hypothesis': '待验证假说', 'supported': '提案标为有支持（须核验实际证据）', 'refuted': '提案标为被反驳（须核验实际证据）'}
    for position, entry in enumerate(_items(story.get('claims')), 1):
        item = _object(entry)
        lines.extend(['', f'### 贡献主张 {position}', '', _text(item.get('claim')),
                      '- 当前状态：' + status.get(item.get('status'), '未记录'),
                      '- 依据文献标识：' + ('、'.join(_text(value) for value in _items(item.get('evidence_ids'))) or '未记录'),
                      '- 验证与反驳计划：' + _text(item.get('validation_plan'))])
    bullets('文章结构', story.get('outline'))
    bullets('候选标题', story.get('title_candidates'))
    bullets('尚未补齐的发表证据', proposal.get('publication_gap'))
    lines.extend(['', '叙事应沿“问题—缺口—假设—方法—实验—边界”展开；不得把待验证的效果写成既成事实，或仅靠换名包装已有方法。', ''])
    return '\n'.join(lines)
