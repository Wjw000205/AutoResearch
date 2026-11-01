"""Scientific compression safeguards, independent of any model backend.

Protection is conservative and deterministic, not a semantic completeness
certificate. Raw sources remain authoritative, including undetected caveats.
"""
from __future__ import annotations

import copy
import hashlib
import re


POLICY_VERSION = 1
CLASSES = {
    'A': 'Exact numbers, conditions, observations and source anchors; no lossy rewriting.',
    'B': 'Counterevidence, limitations, negative results, anomalies and uncertainty; strongly protected.',
    'C': 'Claims and method descriptions; structured extraction with provenance.',
    'D': 'Background and duplication; select or externalize with archive pointers.',
    'E': 'Intermediate chatter; omit from working context, keep the audit archive.',
}
ADVERSE_KINDS = frozenset({'counterevidence', 'limitation', 'negative_result', 'failed_to_replicate',
    'alternative_explanation', 'anomaly', 'uncertainty'})
ADVERSE_FIELDS = frozenset({'counterevidence', 'contradicts', 'contradicting_evidence', 'contradiction',
    'negative_results', 'anomalies', 'failed_to_replicate', 'alternative_explanations',
    'limitations', 'uncertainties', 'assumptions', 'unresolved', 'unresolved_issues', 'unresolved_conflicts',
    'open_questions', 'gaps', 'missing_information', 'required_changes', 'objections', 'error', 'errors'})
# Pin explicit negative findings across catalog relevance filters. Ordinary
# limitations also get an independent candidate quota, without loading all papers.
PINNED_FIELDS = ADVERSE_FIELDS - {'limitations', 'missing_information', 'gaps', 'assumptions', 'uncertainties'}
EXACT_FIELDS = frozenset({'conditions', 'result', 'results', 'measurements', 'observed_results',
    'numerical_results', 'effect_size', 'p_value', 'sample_size', 'dose', 'duration',
    'source_anchor', 'source_anchors', 'quote', 'locator'})
ADVERSE_TEXT = re.compile(r'(?i)\b(?:fail\w*|limitation\w*|however|worse|confound\w*|contradict\w*|'
    r'negative result\w*|not significant|no significant|alternative explanation\w*|anomal\w*|'
    r'uncertain\w*|speculat\w*|hypothesi\w*|may|might|could|suggest\w*)\b|'
    r'局限|失败|不显著|混杂|反例|反证|异常|不确定|可能|推测|假设|未能|无效')
CONDITION_TEXT = re.compile(r'(?i)\b(?:dose|dosage|duration|temperature|concentration|population|'
    r'sample size|cell line|condition\w*|control\w*|baseline\w*|dataset\w*|measurement\w*|'
    r'protocol\w*|assum\w*|only|unless|without)\b|剂量|时长|温度|浓度|样本|条件|对照|基线|数据集|仅在|只有|除非')
# Decimal points are deliberately not sentence separators. Preserve adjacent
# qualifiers by retaining the neighboring sentence of every detected span.
SENTENCE_END = re.compile(r'(?<=[.!?。！？；;])\s+|[\r\n]+|(?<=[。！？；])')


def protected(value, *, exact=False):
    """Recognize nested structured facts; never infer scientific validity."""
    if isinstance(value, list):
        return any(protected(item, exact=exact) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get('approve') is False or value.get('approved') is False:
        return True
    if (isinstance(value.get('kind'), str) and value['kind'] in ADVERSE_KINDS
            or value.get('compression_class') in ('A', 'B')):
        return True
    fields = ADVERSE_FIELDS | (EXACT_FIELDS if exact else frozenset())
    return any((key in fields and item not in (None, '', [], {})) or protected(item, exact=exact)
               for key, item in value.items())


def pinned(value):
    if not isinstance(value, dict):
        return False
    return (value.get('compression_class') in ('A', 'B')
            or isinstance(value.get('kind'), str) and value['kind'] in ADVERSE_KINDS
            or any(value.get(key) for key in PINNED_FIELDS))


def exact_spans(text, source_id, sha, offset=0):
    """Keep complete, anchored A/B candidate spans without a truncation cap.

    Overlapping neighboring windows are merged, preserving byte-for-byte text.
    If this retains a whole technical page it must stay whole or fail the budget.
    """
    bounds, start = [], 0
    for match in SENTENCE_END.finditer(text):
        if match.end() > start:
            bounds.append((start, match.end()))
        start = match.end()
    if start < len(text):
        bounds.append((start, len(text)))
    selected = {}
    for index, (start, end) in enumerate(bounds):
        part = text[start:end]
        classes = set()
        if re.search(r'\d', part) or CONDITION_TEXT.search(part):
            classes.add('A')
        if ADVERSE_TEXT.search(part):
            classes.add('B')
        if classes:
            for neighbor in range(max(0, index-1), min(len(bounds), index+2)):
                if neighbor == index or bounds[neighbor][1]-bounds[neighbor][0] <= 1200:
                    selected.setdefault(neighbor, set()).update(classes)
    merged = []
    for index, classes in sorted(selected.items()):
        start, end = bounds[index]
        if merged and merged[-1][1] == start:
            merged[-1][1] = end
            merged[-1][2].update(classes)
        else:
            merged.append([start, end, set(classes)])
    return [{'source_id': source_id, 'text_sha256': sha,
             'locator': f'{source_id}#chars={offset+start}:{offset+end}',
             'quote': text[start:end], 'classes': sorted(classes),
             'provenance': 'deterministic_exact_source_span'} for start, end, classes in merged]


def protect_card(card, page):
    """Rebind a validated extraction to the current raw receipt, never a summary."""
    if not isinstance(card, dict):
        raise ValueError('Evidence card must be a structured object')
    if (card.get('source_id') != page['source_id'] or card.get('text_sha256') != page['text_sha256']
            or card.get('original_offset') != page['offset'] or card.get('original_chars') != len(page['text'])):
        raise ValueError('Evidence card does not match the current source page')
    if not isinstance(card.get('findings'), list) or not card['findings']:
        raise ValueError('Evidence card has no anchored findings')
    for finding in card['findings']:
        if not isinstance(finding, dict):
            raise ValueError('Evidence finding must be a structured object')
        start = finding.get('offset')
        quote = finding.get('quote')
        if (type(start) is not int or start < 0 or not isinstance(quote, str) or not quote
                or page['text'][start:start+len(quote)] != quote
                or finding.get('locator') != f"{page['source_id']}#chars={page['offset']+start}:{page['offset']+start+len(quote)}"):
            raise ValueError('Evidence card quote does not match the current source page')
    result = copy.deepcopy(card)
    result['protected_source_spans'] = exact_spans(page['text'], page['source_id'], page['text_sha256'], page['offset'])
    result['compression_safety'] = {'version': POLICY_VERSION,
        'source_page_sha256': hashlib.sha256(page['text'].encode()).hexdigest(),
        'classification': 'Conservative lexical A/B detection plus neighboring sentences; not semantic coverage.',
        'scientific_claims_verified': False, 'omission_risk': 'Unrecognized facts remain in the raw source; expand before decisive judgments.'}
    result['expansion'] = {'tool': 'evidence.expand', 'span': {'source_id': page['source_id'],
        'expected_sha256': page['text_sha256'], 'offset': page['offset'], 'limit': min(len(page['text']), 16000)},
        'end_offset': page['offset']+len(page['text']), 'continue_until_end': True}
    return result


# Numbers in bibliography/section labels are not measurements. A quantitative
# value needs a unit, statistical notation, interval or explicit measured noun.
MEASUREMENT = re.compile(r'(?i)(?:\d(?:[\d.,]*\d)?\s*(?:%|±|°[CF]|μM|uM|mM|mg|µg|ug|ml|mm|cm|kg|'
    r'ms\b|seconds?\b|minutes?\b|hours?\b|days?\b|epochs?\b|tokens?\b)|'
    r'\b[NP]\s*[<=>]\s*\d|\b(?:accuracy|rmse|mae|mse|auc|f1|sample size|dose|temperature|'
    r'concentration|confidence interval|effect size)\b[^.!?\n]{0,90}\d|'
    r'\d[^。！？\n]{0,20}(?:摄氏|小时|分钟|毫克|微摩尔)|(?:样本量|置信区间|准确率|误差|剂量)[^。！？\n]{0,60}\d)')
DECISIVE_CAVEAT = re.compile(r'(?i)\b(?:fail\w*|not significant|no significant|worse|confound\w*|'
    r'contradict\w*|negative result\w*|limitation\w*|uncertain\w*|alternative explanation|'
    r'only when|unless)\b|失败|未能|未复现|不显著|反证|反例|局限|混杂|不确定|仅在|除非')
QUALIFIER = re.compile(r'(?i)^\s*(?:(?:this|it|these|those|however|but)\b[^.!?\n]{0,100}\b'
    r'(?:may|might|could|suggest\w*|reflect)|(?:这|该结果|这些|然而)[^。！？\n]{0,70}(?:可能|推测|提示))')


def decisive_spans(text, source_id, offset=0):
    """Exact measured/adverse clauses and attached qualifiers, with locators."""
    spans, start = [], 0
    previous_measured = False
    for match in [*SENTENCE_END.finditer(text), None]:
        end = match.end() if match else len(text)
        part = text[start:end]
        classes = []
        if MEASUREMENT.search(part):
            classes.append('A')
        if DECISIVE_CAVEAT.search(part):
            classes.append('B')
        if previous_measured and len(part) <= 1200 and QUALIFIER.search(part):
            classes.append('B')
        if classes and part:
            spans.append({'quote': part, 'classes': classes,
                'locator': f'{source_id}#chars={offset+start}:{offset+end}'})
        start = end
        previous_measured = 'A' in classes
    return spans


def compact_card(card, page):
    """Validated facts + exact decisive clauses, without neighboring prose.

This is a working view only. Semantic completeness is not asserted; originals
and the legacy validated extraction remain unchanged and expandable.
"""
    result = protect_card(card, page)
    # An exact finding quote already supplies this span without a copy.
    result['protected_source_spans'] = [span for span in decisive_spans(page['text'], page['source_id'], page['offset'])
        if not any(span['quote'].strip() in finding['quote'] for finding in result['findings'])]
    result['compression_safety'].update(version=2,
        classification='Exact anchored findings/conditions/values and decisive clauses; no neighboring sentence expansion.',
        omission_risk='Other details stay in the source archive. Expand the original before a decisive claim requiring undisplayed context.')
    return result
