"""One paper contract for scholarly discovery; metadata is not read evidence."""
from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from urllib.parse import unquote


def canonical_doi(value):
    value = re.sub(r'^https?://(?:dx\.)?doi\.org/|^doi:\s*', '',
                   unquote(str(value or '')).strip(), flags=re.I).lower()
    return value if re.fullmatch(r'10\.\d{4,9}/[^\s\x00-\x1f]+', value) else None


def normalized_title(value):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', str(value or '')).casefold()))


def canonical_arxiv(value):
    value = re.sub(r'^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/|^arxiv:', '', str(value or ''), flags=re.I).removesuffix('.pdf')
    return re.sub(r'v\d+$', '', value) if re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?', value) else None


@dataclass
class Paper:
    paper_id: str
    title: str
    abstract: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    arxiv_id: str | None = None
    semantic_scholar_id: str | None = None
    journal: str | None = None
    citation_count: int | None = None
    sources: list[str] = field(default_factory=list)
    oa_pdf_url: str | None = None
    relevance_score: float = 0.0
    metadata: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_record(cls, record):
        record = copy.deepcopy(record)
        title = str(record.get('title') or '').strip()
        doi = canonical_doi(record.get('doi'))
        identifier = str(record.get('id') or record.get('paper_id') or doi or
                         'title:' + hashlib.sha256(normalized_title(title).encode()).hexdigest()[:24])
        sources = sorted(set(record.get('sources') or []) | ({record['source']} if record.get('source') else set()))
        count = record.get('citation_count')
        journal = record.get('journal') or next(iter((record.get('venue') or {}).get('names') or []), None)
        return cls(identifier, title, record.get('abstract') or record.get('abstract_excerpt'),
                   list(record.get('authors') or []), record.get('year'), doi,
                   str(record['pmid']) if record.get('pmid') else None, record.get('pmcid'),
                   record.get('arxiv_id'), record.get('semantic_scholar_id'), journal,
                   count if type(count) is int and count >= 0 else None, sources,
                   record.get('oa_pdf_url'), float(record.get('relevance_score') or 0), record)

    def to_record(self):
        values = asdict(self)
        values.pop('metadata')
        return {**copy.deepcopy(self.metadata), **values, 'id': self.paper_id,
                'source': self.metadata.get('source') or next(iter(self.sources), None)}


def normalize(record):
    return Paper.from_record(record).to_record()
