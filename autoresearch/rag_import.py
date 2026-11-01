"""Bounded external paper packages: no inference, network access or task approvals."""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import re
import sqlite3
import stat
import zipfile
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .paper_rag import PaperRAG, aliases, digest
from .storage import Store, now

MAX_PACKAGE = 64 * 1024 * 1024
MAX_ORIGINAL = 12 * 1024 * 1024
Text = Annotated[str, Field(min_length=1, max_length=2400)]
Hash = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)


class Producer(Contract):
    agent: Text
    model: Text
    analyzed_at: datetime

    @field_validator('analyzed_at')
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError('analyzed_at requires a timezone')
        return value


class Paper(Contract):
    title: Text
    authors: list[Text] = Field(min_length=1, max_length=200)
    year: int = Field(ge=1900, le=2100, strict=True)
    publication_date: date | None = None
    doi: str | None = Field(default=None, max_length=500)
    dblp_key: str | None = Field(default=None, max_length=500)
    url: Text
    venue: Text
    track: Text
    ccf_catalog_version: Text
    ccf_abbr: Text
    proceedings_url: Text
    ccf_domain: Text | None = None
    ccf_domain_source_url: Text | None = None
    research_topics: list[Text] = Field(default_factory=list, max_length=20)
    keywords: list[Text] = Field(default_factory=list, max_length=40)

    @field_validator('ccf_domain_source_url')
    @classmethod
    def domain_source_url(cls, value):
        return cls.public_url(value) if value is not None else None

    @field_validator('ccf_domain', 'research_topics', 'keywords')
    @classmethod
    def classification_labels(cls, value):
        if value is None:
            return value
        labels = value if isinstance(value, list) else [value]
        if any(not label.strip() for label in labels):
            raise ValueError('Classification labels cannot be blank')
        cleaned = list(dict.fromkeys(label.strip() for label in labels))
        return cleaned if isinstance(value, list) else cleaned[0]

    @field_validator('url', 'proceedings_url')
    @classmethod
    def public_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Expected HTTP(S) source URL without credentials')
        return value

    @model_validator(mode='after')
    def identity(self):
        if bool(self.ccf_domain) != bool(self.ccf_domain_source_url):
            raise ValueError('CCF domain and its official source URL must be supplied together')
        if not self.doi and not self.dblp_key:
            raise ValueError('At least one stable identifier is required: doi or dblp_key')
        if self.doi:
            self.doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', self.doi.strip(), flags=re.I).lower()
            if not re.fullmatch(r'10\.\d{4,9}/\S+', self.doi):
                raise ValueError('Invalid DOI')
        if self.dblp_key and not re.fullmatch(r'(?:conf|journals)/[^\s?#]+', self.dblp_key):
            raise ValueError('Use a DBLP record key, such as conf/icml/Example23')
        if self.year > date.today().year or self.publication_date and (
                self.publication_date > date.today() or self.publication_date.year != self.year):
            raise ValueError('Publication date/year must agree and cannot be in the future')
        return self


class Source(Contract):
    original_file: Text
    text_file: Text
    original_sha256: Hash
    text_sha256: Hash
    url: Text
    format: Literal['pdf', 'html', 'text']
    extraction_method: Text
    coverage: Literal['full_text', 'partial', 'abstract_only']
    missing_parts: list[Text] = Field(max_length=100)

    _url = field_validator('url')(Paper.public_url.__func__)


class Citation(Contract):
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    quote: str = Field(min_length=20, max_length=3200)


class Result(Citation):
    dataset: Text
    metric: Text
    value: Text
    setting: Text


class Section(Contract):
    topic: Literal['problem', 'method', 'training', 'inference', 'experiments', 'ablations',
                   'resources', 'negative_results', 'limitations', 'related_work', 'theory']
    text: Text
    epistemic_status: Literal['author_report', 'author_interpretation', 'analyst_inference']
    citations: list[Citation] = Field(min_length=1, max_length=12)


class Analysis(Contract):
    summary: Text
    research_question: Text
    method: Text
    relevance: Text
    datasets: list[Text] = Field(max_length=32)
    baselines: list[Text] = Field(max_length=32)
    results: list[Result] = Field(max_length=12)
    limitations: list[Text] = Field(max_length=32)
    missing_information: list[Text] = Field(max_length=32)
    excerpts: list[Citation] = Field(min_length=1, max_length=12)
    sections: list[Section] = Field(min_length=1, max_length=32)


class ImportPaper(Contract):
    item_id: str = Field(pattern=r'^[A-Za-z0-9._-]{1,120}$')
    paper: Paper
    source: Source
    producer: Producer
    analysis: Analysis


class Package(Contract):
    schema_version: Literal[1]
    papers: list[ImportPaper] = Field(min_length=1, max_length=20)


def schema():
    return Package.model_json_schema()


def _strict_json(body):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError('Duplicate JSON key')
            result[k] = v
        return result
    def invalid(_):
        raise ValueError('Non-finite JSON number')
    return json.loads(body, object_pairs_hook=pairs, parse_constant=invalid)


def _archive(body):
    if len(body) > MAX_PACKAGE:
        raise ValueError('ZIP exceeds 64 MiB')
    archive = zipfile.ZipFile(io.BytesIO(body))
    infos = archive.infolist()
    if len(infos) > 100 or sum(i.file_size for i in infos) > MAX_PACKAGE:
        archive.close()
        raise ValueError('ZIP exceeds 100 entries or 64 MiB uncompressed')
    names = set()
    for info in infos:
        name = info.orig_filename
        if (info.filename in names or not name or '\\' in name or ':' in name
                or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
            archive.close()
            raise ValueError('Unsafe, duplicate or encrypted ZIP member')
        names.add(info.filename)
    return archive


def _prepare(raw, archive):
    item = ImportPaper.model_validate(raw)
    source = item.source
    original = archive.read(source.original_file)
    text_body = archive.read(source.text_file)
    if not original or len(original) > MAX_ORIGINAL:
        raise ValueError('Original must contain 1..12 MiB; oversized sources need an explicit importer upgrade')
    if hashlib.sha256(original).hexdigest() != source.original_sha256 or hashlib.sha256(text_body).hexdigest() != source.text_sha256:
        raise ValueError('Source SHA256 mismatch')
    if (source.format == 'pdf') != original.startswith(b'%PDF-'):
        raise ValueError('Declared source format does not match PDF signature')
    text = text_body.decode('utf-8')
    if not text.strip() or len(text) > 150000:
        raise ValueError('Text must contain 1..150000 characters; do not silently truncate')
    if source.coverage != 'full_text' and not source.missing_parts:
        raise ValueError('Partial/abstract sources must disclose missing_parts')
    refs = [*item.analysis.excerpts, *item.analysis.results,
            *(ref for section in item.analysis.sections for ref in section.citations)]
    for ref in refs:
        if ref.end <= ref.start or text[ref.start:ref.end] != ref.quote:
            raise ValueError('Citation must match the exact UTF-8 decoded source character range')
    paper = item.paper.model_dump(mode='json', exclude_none=True)
    paper_id = 'doi:' + paper['doi'] if paper.get('doi') else 'dblp:' + paper['dblp_key']
    paper.update(id=paper_id, verified=False, verification={'status': 'unverified', 'method': 'external_submission'},
                 venue={'names': [item.paper.venue], 'track': item.paper.track, 'kind': 'conference'},
                 import_provenance='external_agent_analysis')
    # Catalog matching is a useful hint, never a fabricated venue verification.
    from .venue_policy import assess
    candidate = assess(paper, candidate=True)
    paper['ccf_candidate'] = candidate
    paper['ccf'] = assess(paper)
    source_id = 'external:' + source.text_sha256
    analysis = item.analysis.model_dump(mode='json')
    note = {k: v for k, v in analysis.items() if k not in ('sections', 'results', 'excerpts')}
    for field in ('results', 'excerpts'):
        note[field] = [{**{k: v for k, v in ref.items() if k not in ('start', 'end')},
                        'locator': f'{source_id}#chars={ref["start"]}:{ref["end"]}'} for ref in analysis[field]]
    note.update(paper_id=paper_id, source_id=source_id, text_sha256=source.text_sha256)
    from .literature_notes import _schema
    _schema(note)  # Same field and reported-value checks as native notes; no reading receipts manufactured.
    producer = item.producer.model_dump(mode='json')
    note.update(generation='external_agent_analysis', created_at=producer['analyzed_at'],
                reading_scope='externally_reported_passages', scientific_claims_verified=False,
                grants_task_reading_credit=False, grants_novelty_approval=False,
                producer=producer, sections=analysis['sections'])
    document = {'text': text, 'text_sha256': source.text_sha256, 'content_sha256': source.original_sha256,
                'url': source.url, 'format': source.format, 'title': paper['title'], 'pages': [],
                'text_truncated': source.coverage != 'full_text', 'extraction_method': source.extraction_method,
                'reported_coverage': source.coverage, 'missing_parts': source.missing_parts,
                'extraction_status': 'externally_supplied_text', 'provenance': 'external_agent_source',
                'untrusted': True, 'verified': False, 'original_bytes_available': True,
                'extraction_independently_verified': False}
    original_name = source.original_sha256 + ('.pdf' if source.format == 'pdf' else '.bin')
    payload = {'paper': paper, 'note': note, 'document': document, 'original': original_name,
               'origin_run_id': None, 'origin_note_id': digest(note), 'origin_source_id': source_id,
               'analysis_created_at': producer['analyzed_at'], 'provenance': 'external_agent_analysis'}
    return item, payload, original


class _IndexTransaction:
    def __init__(self, db):
        self.db = db

    def get(self, collection, key, default=None):
        row = self.db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, collection, key, value):
        self.db.execute('INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(collection,key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                        (collection, key, json.dumps(value, ensure_ascii=False, allow_nan=False), now()))
        return value

    def batch(self, puts):
        for collection, key, value in puts:
            self.put(collection, key, value)


def _publish(rag, payload, original, *, dry_run):
    from .intelligence import Intelligence
    from .paper_identity import Transaction, register_in
    identifiers = aliases(payload['paper'])
    # The write transaction serializes independent CLI/server writers as well.
    with rag.store._db() as db:
        db.execute('BEGIN' if dry_run else 'BEGIN IMMEDIATE')
        view = Transaction(db)
        entry_id = register_in(view, payload['paper'])
        rag._reconcile_source_aliases(view, entry_id)
        old = view.get('paper_rag', entry_id)
        if old:
            from .venue_policy import normalize
            if normalize(old['paper']['title']) != normalize(payload['paper']['title']):
                raise ValueError('Identifier/title conflict with existing paper; resolve explicitly')
        payload = {**payload, 'entry_id': entry_id}
        version_id = digest(payload)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        if len(body) > 2 * 1024 * 1024:
            raise ValueError('Paper snapshot exceeds existing RAG 2 MiB bound')
        result = {'entry_id': entry_id, 'version_id': version_id}
        present = version_id if view.get('paper_rag_versions', version_id) else next(
            (v for v in (old or {}).get('versions', []) if (record := view.get('paper_rag_versions', v))
             and digest({**payload, 'entry_id': record['entry_id']}) == v), None)
        if present:
            if dry_run:
                db.rollback()
            rag.load(present)
            result['version_id'] = present
            return {**result, 'status': 'already_present'}
        if dry_run:
            db.rollback()
            return {**result, 'status': 'validated'}
        rag._write(payload['original'], original)
        rag._write(version_id + '.json', body)
        indexer = Intelligence(view, {})
        meta = {'kind': 'external_paper_analysis', 'shared': True, 'rag_entry_id': entry_id,
                'rag_version_id': version_id, 'provenance': 'external_agent_analysis',
                'scientific_claims_verified': False, 'grants_task_reading_credit': False}
        memory = indexer.memory_add('global', 'shared', json.dumps(payload['paper'], ensure_ascii=False) + '\n' + json.dumps(payload['note'], ensure_ascii=False), meta)
        rag._index_source(view, entry_id, version_id, payload['document'])
        view.put('paper_rag_versions', version_id, {'id': version_id, 'entry_id': entry_id,
            'snapshot': version_id + '.json', 'snapshot_sha256': hashlib.sha256(body).hexdigest(),
            'created_at': now(), 'origin_run_id': None, 'memory_document_id': memory['document_id'],
            'provenance': 'external_agent_analysis'})
        for alias in identifiers:
            view.put('paper_rag_aliases', digest(alias), {'id': digest(alias), 'alias': alias, 'entry_id': entry_id})
        # External submissions cannot displace an existing locally verified default.
        latest = not old or (old.get('provenance') == 'external_agent_analysis'
                             and payload['analysis_created_at'] >= old['analysis_created_at'])
        entry = {**(old or {}), 'id': entry_id, 'versions': sorted(set((old or {}).get('versions', [])) | {version_id}),
                 'created_at': (old or {}).get('created_at', now()), 'updated_at': now()}
        if latest:
            entry.update(paper=payload['paper'], latest_version=version_id,
                         analysis_created_at=payload['analysis_created_at'], provenance='external_agent_analysis')
        view.put('paper_rag', entry_id, entry)
        return {**result, 'status': 'imported'}


def import_package(rag, body: bytes, *, dry_run=True):
    """Each paper commits atomically. Re-submit the same package after interruption."""
    try:
        archive = _archive(body)
        with archive:
            if archive.getinfo('manifest.json').file_size > 4 * 1024 * 1024:
                raise ValueError('Manifest exceeds 4 MiB')
            manifest = _strict_json(archive.read('manifest.json').decode('utf-8'))
            if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'papers'}
                    or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
                    or not isinstance(manifest['papers'], list) or not 1 <= len(manifest['papers']) <= 20):
                raise ValueError('Expected schema_version=1 and 1..20 papers')
            ids = [p.get('item_id') if isinstance(p, dict) else None for p in manifest['papers']]
            if any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
                raise ValueError('Every item_id must be present and unique')
            results = []
            for raw in manifest['papers']:
                try:
                    item, payload, original = _prepare(raw, archive)
                    result = _publish(rag, payload, original, dry_run=dry_run)
                    result['ccf_candidate'] = payload['paper']['ccf_candidate']
                    result['identity_verified'] = False
                except ValidationError as exc:
                    result = {'status': 'failed', 'errors': [
                        {'path': '.'.join(map(str, e['loc'])), 'type': e['type']} for e in exc.errors()]}
                except (ValueError, KeyError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
                    result = {'status': 'failed', 'error': str(exc)[:500]}
                results.append({'item_id': raw['item_id'], **result})
    except (zipfile.BadZipFile, KeyError, UnicodeError) as exc:
        raise ValueError('Invalid ZIP or manifest encoding/missing manifest.json') from exc
    report = {'id': hashlib.sha256(body).hexdigest(), 'schema_version': 1, 'dry_run': dry_run,
              'results': results, 'counts': {s: sum(r['status'] == s for r in results)
                                          for s in ('validated', 'imported', 'already_present', 'failed')},
              'scientific_claims_verified': False, 'grants_task_reading_credit': False}
    if not dry_run:
        rag.store.put('paper_rag_imports', report['id'], {**report, 'updated_at': now()})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--commit', action='store_true', help='Persist valid papers; default validates only')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--schema', action='store_true')
    args = parser.parse_args()
    if args.schema:
        print(json.dumps(schema(), ensure_ascii=False, indent=2))
        return
    if not args.package:
        parser.error('--package is required unless --schema is used')
    from .config import DATA_DIR
    root = (args.data_dir or DATA_DIR).resolve()
    if args.package.stat().st_size > MAX_PACKAGE:
        parser.error('ZIP exceeds 64 MiB')
    rag = PaperRAG(Store(root / 'research.sqlite3'), root)
    try:
        report = import_package(rag, args.package.read_bytes(), dry_run=not args.commit)
    except ValueError as exc:
        parser.error(str(exc))
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(output, encoding='utf-8')
    print(output)
    if report['counts']['failed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
