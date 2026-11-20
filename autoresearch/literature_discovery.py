"""Frozen discovery order: external papers first, matched historical notes second."""
import copy
import re

POLICY = 'latest_external_then_rag_notes'
GUIDANCE = ('\n文献顺序契约（用户更新）：此顺序优先于历史intake中未区分论文与笔记的RAG优先表述。'
    '先从外部学术来源搜索最新论文，按当前问题、发表日期、出处和机制覆盖选择阅读对象，'
    '优先近6个月、主体近12个月；第二轮按候选相关性检索，不限年代。先确定拟阅读论文ID，再调用'
    'literature.read_papers 为这些论文匹配并复用合格RAG笔记。RAG是笔记复用层，不是本轮选题或论文清单来源。'
    '不得因为有现成笔记、容易下载或已读过就优先选择；不得用RAG旧论文替换尚未取得正文的最新核心论文。'
    '匹配须核对同篇身份、来源版本、原文哈希、分析日期和已读范围；不匹配或证据不足才补读并保存新笔记。'
    '保留获取失败、待查论文和覆盖缺口；历史来源ID不一定属于本轮，使用read_papers返回的本轮source_id与text_sha256。'
    '已有笔记复用不产生本轮新阅读、科学验证或实验批准。用户明确上传的seed仍按上传契约单列。')


def enabled(value):
    return (value or {}).get('literature_discovery_policy') == POLICY


def selection_metadata(paper):
    """Keep the externally discovered record; hide note availability from selection."""
    return {key: copy.deepcopy(value) for key,value in paper.items()
            if not key.startswith('rag_') and key != 'retrieval_origin'}


def order(papers):
    # All records already passed domain/date/venue admission. Latest publication
    # takes precedence here; citation counts and note availability cannot win.
    return sorted(papers, key=lambda p: (p.get('publication_date') or '', p.get('relevance_score', 0)), reverse=True)


def source_matches(paper, payload):
    """An explicit source version/hash cannot be satisfied by another revision."""
    document = payload['document']
    for key in ('text_sha256', 'content_sha256'):
        if paper.get(key) and paper[key] != document.get(key):
            return False

    def versions(value):
        fields = (value.get(k) for k in ('arxiv_id', 'id', 'url', 'original_url', 'fulltext_url'))
        return set(re.findall(r'(\d{4}\.\d{4,5}v\d+|[a-zA-Z.-]+/\d{7}v\d+)',
                              ' '.join(str(v or '') for v in fields)))
    expected = versions(paper)
    available = versions(document) or versions(payload.get('paper', {}))
    return not expected or expected <= available
