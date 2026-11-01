"""CCF scope is enforced from registry venue metadata, never model-supplied ranks."""
from __future__ import annotations

import copy
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

VERSION = 'CCF-2026-7'
INSTRUCTION = ('文献范围限定CCF第七版目录A/B类会议和期刊。两轮分析、核心/待读名单和RAG复用均受此约束。'
    '只依据工具返回的已核验venue/ccf字段判断；不得从标题、出版社、模型记忆或自报等级认定。'
    'C类、未收录、出处未核实、纯预印本，以及Workshop/Findings/Short/Demo等非正式长文不能入选。'
    '不足时调整检索式继续找合格论文，不能用不合格论文凑数。此范围不代表单篇论文的结论或创新性已被验证。')


def enabled(run):
    return bool(run and run.get('mode') != 'demo' and run.get('config', {}).get('ccf_ab_only') is True)


def policy(run):
    return {'enabled': enabled(run), 'allowed_ranks': ['A', 'B'], 'catalog_version': VERSION,
            'source_url': catalog()['announcement'], 'scope': INSTRUCTION}


@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name('ccf_catalog.json').read_text(encoding='utf-8'))


def normalize(value):
    value = unicodedata.normalize('NFKC', str(value)).casefold().replace('&', 'and')
    value = re.sub(r'\b(?:19|20)\d{2}\b|\b\d+(?:st|nd|rd|th)\b', ' ', value)
    value = re.sub(r'\b(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)[ -])?(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|nineteenth|twentieth|thirtieth|fortieth|fiftieth)\b', ' ', value)
    value = re.sub(r'^\s*(?:proceedings\s+of\s+)?(?:the\s+)?', '', value)
    return re.sub(r'[^a-z0-9]', '', value)


@lru_cache(maxsize=1)
def _index():
    index = {}
    for row in catalog()['entries']:
        for name in [row['name'], row['name'] + ' (' + row['abbr'] + ')',
                     *([row['abbr']] if len(row['abbr']) >= 3 else [])]:
            index.setdefault(normalize(name), []).append(row)
    # Alternate titles of the same proceedings, not substring matches.
    aliases = {'Advances in Neural Information Processing Systems': 'NeurIPS',
        'Neural Information Processing Systems': 'NeurIPS',
        'Proceedings of the AAAI Conference on Artificial Intelligence': 'AAAI',
        'International Joint Conference on Artificial Intelligence': 'IJCAI',
        'IEEE Transactions on Knowledge and Data Engineering': 'TKDE',
        'ACM SIGKDD Conference on Knowledge Discovery and Data Mining': 'KDD',
        'IEEE Conference on Computer Vision and Pattern Recognition': 'CVPR',
        'IEEE/CVF Conference on Computer Vision and Pattern Recognition': 'CVPR'}
    for alias, abbr in aliases.items():
        index[normalize(alias)] = [r for r in catalog()['entries'] if r['abbr'] == abbr]
        index[normalize(alias + ' (' + abbr + ')')] = index[normalize(alias)]
    renamed_ir = [r for r in catalog()['entries'] if r['url'].endswith('/journals/ir/')]
    for alias in ('Discover Computing', 'Information Retrieval Journal', 'Information Retrieval'):
        index[normalize(alias)] = renamed_ir
    return index


def assess(paper, *, candidate=False):
    verification = paper.get('verification') or {}
    # Candidate filtering is only for fetched registry search rows; admission
    # always requires a fresh identifier lookup and its canonical venue binding.
    venue = paper.get('venue') if candidate else verification.get('venue_metadata')
    base = {'catalog_version': VERSION, 'source_url': catalog()['announcement'], 'rank': None,
            'eligible': False, 'reason': 'venue_unverified'}
    if not candidate and (paper.get('verified') is not True or verification.get('status') != 'verified'
            or verification.get('method') not in {'crossref_doi_registry', 'openalex_index_record'}):
        return base
    if not isinstance(venue, dict) or not venue.get('names'):
        return base
    base['venue_names'] = venue['names']
    base['evidence_url'] = verification.get('evidence_url')
    label = ' '.join([*venue['names'], str(venue.get('track') or ''), str(venue.get('publication_type') or '')])
    if re.search(r'\b(workshops?|findings|short(?:\s+papers?)?|demos?|demonstrations?|technical\s+brief|summary|companion|preprint|posted-content)\b', label, re.I):
        return {**base, 'reason': 'excluded_publication_track'}
    matches = {}
    for name in venue['names']:
        for row in _index().get(normalize(name), []):
            matches[(row['name'], row['kind'])] = row
    # Some conference proceedings (e.g. AAAI) are registered as a journal
    # series by Crossref. Exact, unique full venue names take precedence.
    if len(matches) > 1 and venue.get('kind') in {'journal', 'conference'}:
        matches = {k: v for k, v in matches.items() if v['kind'] == venue['kind']}
    if len(matches) != 1:
        return {**base, 'reason': 'venue_not_listed' if not matches else 'venue_ambiguous'}
    row = next(iter(matches.values()))
    # A proceedings chapter named "Pattern Recognition" is not the journal
    # with that name. Crossref's journal-article registration of AAAI remains valid.
    if row['kind'] == 'journal' and venue.get('publication_type') in {'book-chapter', 'book'}:
        return {**base, 'reason': 'venue_kind_mismatch'}
    return {**base, 'rank': row['rank'], 'venue_name': row['name'], 'abbr': row['abbr'],
            'kind': row['kind'], 'catalog_page': row['page'], 'eligible': row['rank'] in {'A', 'B'},
            'domain': row['domain'], 'domain_catalog_page': row['domain_page'],
            'reason': 'eligible' if row['rank'] in {'A', 'B'} else 'ccf_rank_excluded'}


def annotate(paper):
    return {**copy.deepcopy(paper), 'ccf': assess(paper)}


def filter_papers(papers):
    return [annotate(p) for p in papers if assess(p)['eligible']]
