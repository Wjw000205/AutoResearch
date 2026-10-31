"""Shared, content-addressed paper evidence. No model calls or learned embeddings.

Only revalidated live reading notes enter this library. Original source bytes,
text, note evidence and provenance survive deletion of the originating task.
Task reuse is explicit and never manufactures a new agent reading receipt.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from datetime import date, datetime
from pathlib import Path

from .intelligence import Intelligence, _features, _vector
from .storage import RunDeletedError, now
from .deletion import _plain_path
from .paper_identity import aliases, canonical, resolve, register_in, same_paper, Transaction


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


ASPECT_PATTERNS = {
    'architecture': r'架构|网络结构|模型结构|方法结构|框架|\barchitecture\b|\barchitectural\b|\bmodel structure\b',
    'contributions': r'创新点|主要创新|创新|主要贡献|贡献|\binnovations?\b|\bnovelty\b|\bcontributions?\b',
    'baselines': r'比较基线|基线|对比方法|\bbaselines?\b',
}


def search_intent(query):
    """Recognize requested evidence fields, keeping the paper/topic as the query."""
    subject, aspects = str(query), []
    for aspect, pattern in ASPECT_PATTERNS.items():
        if re.search(pattern, subject, re.I):
            aspects.append(aspect)
            subject = re.sub(pattern, ' ', subject, flags=re.I)
    if aspects:
        subject = re.sub(r'是什么|有哪些|有什么|是怎样的|怎么样|该论文|这篇论文|这篇|论文|本文|该文|的|和|及|请|什么', ' ', subject)
        subject = re.sub(r'\b(what|are|the|paper|its|describe)\b|[?？:：]', ' ', subject, flags=re.I)
    return ' '.join(subject.split()), aspects


def aspect_evidence(payload, aspects):
    note = payload['note']
    fields = {'architecture': ('method',),
              'contributions': ('research_question', 'summary', 'method', 'baselines'),
              'baselines': ('baselines', 'method', 'results')}
    evidence = {}
    for aspect in aspects:
        evidence[aspect] = {'fields': {field: copy.deepcopy(note.get(field)) for field in fields[aspect]},
            'excerpts': copy.deepcopy(note.get('excerpts') or []),
            'note_id': note.get('id') or payload['origin_note_id'], 'text_sha256': note['text_sha256'],
            'origin_run_id': payload['origin_run_id'], 'analysis_created_at': payload['analysis_created_at'],
            'basis': 'external_analysis_fields' if payload.get('provenance') == 'external_agent_analysis' else 'validated_analysis_fields',
            'provenance': payload.get('provenance'), 'scientific_claims_verified': False,
            'novelty_verified': False}
    return evidence


class PaperRAG:
    def __init__(self, store, data_dir):
        self.store = store
        self.root = Path(data_dir).resolve() / 'paper-rag'
        self.indexer = Intelligence(store, {})  # Local indexing only; no credentials/inference.
        self._publish_lock = threading.RLock()

    def _path(self, name):
        if not re.fullmatch(r'[a-f0-9]{64}\.(json|pdf|bin)', name):
            raise ValueError('Invalid shared paper snapshot name')
        path = self.root / name
        _plain_path(path, self.root.parent)
        return path

    def _write(self, name, body):
        path = self._path(name)
        self.root.mkdir(parents=True, exist_ok=True)
        _plain_path(path, self.root.parent)
        if path.exists():
            if path.read_bytes() != body:
                raise ValueError('Shared paper snapshot was modified')
            return
        fd, temporary = tempfile.mkstemp(prefix='.rag-', suffix='.tmp', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def load(self, version_id):
        version = self.store.get('paper_rag_versions', version_id)
        if not version:
            raise ValueError('Shared paper version does not exist')
        path = self._path(version['snapshot'])
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Shared paper snapshot is unavailable')
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != version['snapshot_sha256']:
            raise ValueError('Shared paper snapshot integrity check failed')
        payload = json.loads(body)
        if digest(payload) != version_id or payload['entry_id'] != version['entry_id']:
            raise ValueError('Shared paper version identity mismatch')
        text = payload['document']['text']
        if hashlib.sha256(text.encode()).hexdigest() != payload['note']['text_sha256']:
            raise ValueError('Shared paper text integrity check failed')
        return payload

    def canonical_id(self, entry_id):
        return canonical(self.store, entry_id)

    def _index_source(self, view, entry_id, version_id, document):
        from .paper_bindings import source_key, SOURCE_IDENTITY_PROTOCOL
        key = source_key(entry_id, document)
        old = view.get('paper_rag_sources', key)
        manifest = Intelligence(view, {}).memory_index_document('global', 'shared',
            {**document, 'rag_entry_id': entry_id, 'rag_version_id': version_id,
             'rag_source_id': key, 'provenance': 'shared_paper_rag'}, source_id='rag-source:' + key)
        view.put('paper_rag_sources', key, {'id': key, 'entry_id': entry_id,
            'source_identity_protocol': SOURCE_IDENTITY_PROTOCOL,
            'source_id': 'rag-source:' + key, 'content_sha256': document.get('content_sha256'),
            'text_sha256': document['text_sha256'], 'memory_document_id': manifest['memory_document_id'],
            'versions': sorted(set((old or {}).get('versions', [])) | {version_id}),
            **({'document_snapshot': document['snapshot']} if document.get('snapshot') else
               ({'document_snapshot': old['document_snapshot']} if old and old.get('document_snapshot') else {})),
            'created_at': (old or {}).get('created_at', now()), 'scientific_claims_verified': False})
        return manifest

    def _reconcile_source_aliases(self, view, entry_id):
        """Merge source indexes in the same transaction as an identity bridge."""
        from .paper_rag_curation import _load as load_curation
        from .paper_bindings import canonical_source, source_key, SOURCE_IDENTITY_PROTOCOL
        for source in view.list('paper_rag_sources'):
            if canonical(view, source['entry_id']) != entry_id or view.get('paper_rag_source_redirects', source['id']):
                continue
            if source['entry_id'] == entry_id and source.get('source_identity_protocol') == SOURCE_IDENTITY_PROTOCOL:
                continue  # A newly published version may still be in this transaction.
            document = (load_curation(self, source['document_snapshot']) if source.get('document_snapshot')
                        else self.load(source['versions'][0])['document'])
            if source['entry_id'] == entry_id and source_key(entry_id, document) == source['id']:
                continue
            target = None
            for version in source['versions']:
                target = self._index_source(view, entry_id, version, document)
            key = target['source_id'].removeprefix('rag-source:')
            view.put('paper_rag_source_redirects', source['id'], {'id': source['id'], 'canonical_id': key, 'created_at': now()})
            previous = view.get('memory_documents', source['memory_document_id'])
            if previous:
                view.put('memory_documents', previous['id'], {**previous, 'superseded_by': target['id'],
                    'canonical_source_id': target['source_id'], 'index_retained_for_replay': True})
        # Owned source snapshots remain intact; their duplicate retrieval index
        # becomes a reference to the same shared original.
        for manifest in view.list('memory_documents'):
            binding = view.get('paper_source_bindings', digest([manifest.get('run_id'), manifest.get('source_id')]))
            if not binding or canonical(view, binding['canonical_paper_id']) != entry_id:
                continue
            source = view.get('paper_rag_sources', canonical_source(view, binding['rag_source_id']))
            if source and source['memory_document_id'] != manifest['id'] and all(
                    source.get(k) == manifest.get(k) for k in ('text_sha256', 'content_sha256')):
                view.put('memory_documents', manifest['id'], {**manifest, 'superseded_by': source['memory_document_id'],
                    'canonical_source_id': source['source_id'], 'index_retained_for_replay': True})

    def index_source(self, entry_id, version_id, document):
        with self.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            view = Transaction(db)
            return self._index_source(view, canonical(view, entry_id), version_id, document)

    def reconcile(self, *, dry_run=True, document_loader=None):
        """Atomically rebuild identities/bindings; preserve every saved version.

        Superseded source indexes remain stored for frozen receipt replay but
        cease participating in retrieval. No inference, downloads or votes.
        """
        from .paper_bindings import bind, source_key, canonical_source, SOURCE_IDENTITY_PROTOCOL
        from .literature_reading import _verified
        payloads = {v['id']: self.load(v['id']) for v in self.store.list('paper_rag_versions')}
        # Check captured original bytes before using a historical source index.
        for payload in payloads.values():
            if payload.get('original'):
                path = self._path(payload['original'])
                if (not path.is_file() or path.stat().st_size > 12 * 1024 * 1024
                        or hashlib.sha256(path.read_bytes()).hexdigest() != payload['document']['content_sha256']):
                    raise ValueError('Shared paper original hash mismatch during reconciliation')
        with self.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            view = Transaction(db)
            before = len(view.list('paper_rag'))
            for entry in view.list('paper_rag'):
                register_in(view, entry['paper'])
            for payload in payloads.values():
                register_in(view, payload['paper'])
            version_sources = {}
            for version_id, payload in payloads.items():
                entry_id = canonical(view, payload['entry_id'])
                manifest = self._index_source(view, entry_id, version_id, payload['document'])
                version_sources[version_id] = manifest
            from .paper_rag_curation import _load as load_curation
            for review in view.list('paper_rag_curation'):
                payload = load_curation(self, review['snapshot'])
                if digest(payload) != review['id']:
                    raise ValueError('Curation snapshot integrity check failed')
                entry_id = canonical(view, payload['entry_id'])
                for ref in payload.get('sources', []):
                    document = load_curation(self, ref['snapshot'])
                    if (canonical(view, document['entry_id']) != entry_id
                            or hashlib.sha256(document['text'].encode()).hexdigest() != ref['text_sha256']):
                        raise ValueError('Curation source integrity check failed')
                    original = self._path(document['original'])
                    if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != document['content_sha256']:
                        raise ValueError('Curation original integrity check failed')
                    self._index_source(view, entry_id, payload['base_version_id'], {**document, 'snapshot': ref['snapshot']})
            superseded = 0
            for source in view.list('paper_rag_sources'):
                if source['versions']:
                    version_id = source['versions'][0]
                    if version_id not in version_sources:
                        raise ValueError('Source index has no immutable analysis version')
                    if source.get('document_snapshot'):
                        document = load_curation(self, source['document_snapshot'])
                        target = source_key(canonical(view, source['entry_id']), document)
                    else:
                        target = version_sources[version_id]['source_id'].removeprefix('rag-source:')
                    if target != source['id']:
                        view.put('paper_rag_source_redirects', source['id'],
                            {'id': source['id'], 'canonical_id': target, 'created_at': now()})
                        previous = view.get('memory_documents', source['memory_document_id'])
                        target_source = view.get('paper_rag_sources', target)
                        if (previous and target_source and target_source['memory_document_id'] != previous['id']
                                and all(target_source.get(k) == previous.get(k)
                                    for k in ('text_sha256', 'content_sha256'))):
                            superseded += 1
                            view.put('memory_documents', previous['id'], {**previous,
                                'superseded_by': target_source['memory_document_id'],
                                'canonical_source_id': target_source['source_id'], 'index_retained_for_replay': True})
            bound, unavailable = 0, 0
            for link in view.list('run_evidence'):
                run_id, paper = link.get('run_id'), link.get('paper')
                if (not run_id or not _verified(paper) or view.get('run_deletions', run_id)
                        or not view.get('runs', run_id)):
                    continue
                entry_id = register_in(view, paper)
                readings = [link.get('reading') or {}] + [r.get('result', {}) for r in link.get('reading_history', []) if isinstance(r, dict)]
                for reading in readings:
                    if not reading.get('source_id') or reading.get('identity_verified') is not True:
                        continue
                    if document_loader:
                        try:
                            document = document_loader(run_id, reading['source_id'])
                            if any(document.get(k) != reading.get(k) for k in ('text_sha256', 'content_sha256')):
                                unavailable += 1
                                continue
                        except (ValueError, KeyError, OSError, TypeError):
                            unavailable += 1
                            continue
                    bound += bool(bind(view, run_id, paper, reading, entry_id=entry_id))
            for manifest in view.list('memory_documents'):
                target = version_sources.get(manifest.get('source_id'))
                if target is None and str(manifest.get('source_id') or '').startswith('curation:'):
                    matches = [source for source in view.list('paper_rag_sources') if not view.get('paper_rag_source_redirects', source['id'])
                        and source['text_sha256'] == manifest.get('text_sha256') and source['content_sha256'] == manifest.get('content_sha256')]
                    old_entry = str(manifest['source_id']).split(':')[1]
                    matches = [s for s in matches if canonical(view, s['entry_id']) == canonical(view, old_entry)]
                    if len(matches) == 1:
                        target = view.get('memory_documents', matches[0]['memory_document_id'])
                if target is None:
                    binding = view.get('paper_source_bindings', digest([manifest.get('run_id'), manifest.get('source_id')]))
                    if binding:
                        source = view.get('paper_rag_sources', canonical_source(view, binding['rag_source_id']))
                        target = view.get('memory_documents', source['memory_document_id']) if source else None
                if (target and target['id'] != manifest['id'] and all(target.get(k) == manifest.get(k)
                        for k in ('text_sha256', 'content_sha256'))):
                    superseded += 1
                    view.put('memory_documents', manifest['id'], {**manifest, 'superseded_by': target['id'],
                        'canonical_source_id': target['source_id'], 'index_retained_for_replay': True})
            report = {'schema_version': 1, 'source_identity_protocol': SOURCE_IDENTITY_PROTOCOL,
                'dry_run': dry_run, 'paper_count_before': before,
                'paper_count': len(view.list('paper_rag')), 'version_count': len(payloads),
                'source_index_count': sum(not view.get('paper_rag_source_redirects', s['id']) for s in view.list('paper_rag_sources')),
                'source_binding_count': len(view.list('paper_source_bindings')), 'binding_receipts_checked': bound,
                'unavailable_receipts': unavailable, 'superseded_source_indexes': superseded,
                'immutable_versions_preserved': True, 'scientific_claims_verified': False, 'model_called': False}
            if dry_run:
                db.rollback()
            else:
                key = digest(report)
                view.put('paper_identity_migrations', key, {'id': key, **report, 'created_at': now()})
            return report

    def publish(self, engine, run_id, notes=None):
        """Idempotent backfill/write-through, validating before sharing anything."""
        # The maintenance thread and an agent saving notes share this instance.
        with self._publish_lock:
            return self._publish(engine, run_id, notes)

    def _publish(self, engine, run_id, notes=None):
        from .literature_notes import notes_view, _memory_text
        from .literature_reading import _run_papers, _verified, uploaded_reading_status
        from .reference_papers import is_uploaded_paper
        run = self.store.get('runs', run_id)
        if not run or run.get('mode') != 'live' or self.store.get('run_deletions', run_id):
            return {'published': 0, 'pending': 0}
        selected = {n['id'] for n in notes} if notes is not None else None
        papers = _run_papers(self.store, run_id)
        valid = notes_view(self.store, run_id, document_loader=engine.sources.document)
        uploaded_state = uploaded_reading_status(engine, run_id)
        uploaded_complete = {row['paper_id']: row for row in uploaded_state['papers']
                             if row['status'] == 'read_and_noted'}
        result = {'published': 0, 'pending': 0}
        for note in valid:
            if note.get('generation') != 'ai_extraction' or selected is not None and note['id'] not in selected:
                continue
            job_id = digest(note['id'])
            previous = self.store.get('paper_rag_jobs', job_id, {})
            if previous.get('status') == 'completed':
                continue
            job = {'id': job_id, 'origin_run_id': run_id, 'origin_note_id': note['id'], 'status': 'pending'}
            self.store.put('paper_rag_jobs', job_id, job)
            try:
                from .paper_identity import task_paper
                paper = task_paper(self.store, run_id, note['paper_id'], papers)
                if not _verified(paper):
                    raise ValueError('Paper identity is not verified')
                uploaded = is_uploaded_paper(self.store, run_id, paper)
                if uploaded and paper['id'] not in uploaded_complete:
                    self.store.put('paper_rag_jobs', job_id, {**job, 'status': 'pending',
                        'reason': 'uploaded_full_reading_incomplete'})
                    result['pending'] += 1
                    continue
                identities = aliases(paper)
                document = engine.sources.document(run_id, note['source_id'])
                source = self.store.get('artifacts', document.get('source_alias_id', note['source_id']))
                # Retain source metadata and pages, without task-local filesystem paths.
                document = {**copy.deepcopy(source['evidence']), 'text': document['text']}
                if document.get('ocr_performed') and document.get('parent_source_id'):
                    parent = engine.sources.document(run_id, document['parent_source_id'])
                    if parent['content_sha256'] != document['content_sha256']:
                        raise ValueError('OCR parent PDF hash changed')
                    document.update(original_bytes_available=parent.get('original_bytes_available'),
                                    original_artifact_id=parent.get('original_artifact_id'))
                original = None
                if document.get('original_bytes_available'):
                    record = self.store.get('artifacts', document['original_artifact_id'])
                    path = Path(record['path'])
                    if not path.resolve().is_relative_to(engine.run_dir(run_id)):
                        raise ValueError('Original paper is outside its task')
                    _plain_path(path, engine.data_dir)
                    if path.stat().st_size > 12 * 1024 * 1024:
                        raise ValueError('Original paper exceeds size bound')
                    body = path.read_bytes()
                    if hashlib.sha256(body).hexdigest() != document['content_sha256']:
                        raise ValueError('Original paper integrity check failed')
                    original = document['content_sha256'] + ('.pdf' if body.startswith(b'%PDF-') else '.bin')
                    self._write(original, body)
                # The CLI importer and every server instance serialize identity
                # assignment with publication, preventing split alias indexes.
                with self.store._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    view = Transaction(db)
                    if not view.get('runs', run_id) or view.get('run_deletions', run_id):
                        raise RunDeletedError('Research has been deleted')
                    if view.get('paper_rag_jobs', job_id, {}).get('status') == 'completed':
                        continue
                    entry_id = register_in(view, paper)
                    self._reconcile_source_aliases(view, entry_id)
                    payload = {'entry_id': entry_id, 'paper': copy.deepcopy(paper), 'note': copy.deepcopy(note),
                        'document': document, 'original': original, 'origin_run_id': run_id,
                        'origin_note_id': note['id'], 'origin_source_id': note['source_id'],
                        'analysis_created_at': note['created_at'], 'provenance': 'validated_live_analysis'}
                    if uploaded:
                        completion = uploaded_complete[paper['id']]
                        payload.update(uploaded_analysis_complete=True,
                            uploaded_reading_completion={k: copy.deepcopy(completion[k]) for k in
                                ('source_id', 'text_sha256', 'content_sha256', 'retained_chars', 'reading_scope', 'scope',
                                 'delivery_required', 'reading_compatibility', 'source_read_event_ids',
                                 'evidence_delivery_receipt_ids')},
                            bibliographic_identity_verified=paper.get('bibliographic_identity_verified') is True)
                    version_id = digest(payload)
                    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
                    self._write(version_id + '.json', body)
                    metadata = {'kind': 'paper_rag_analysis', 'shared': True, 'rag_entry_id': entry_id,
                        'rag_version_id': version_id, 'paper_id': paper['id'], 'title': paper['title'],
                        'publication_date': paper.get('publication_date'), 'origin_run_id': run_id,
                        'origin_note_id': note['id'], 'source_id': note['source_id'],
                        'text_sha256': note['text_sha256'], 'provenance': 'shared_paper_rag',
                        'read_tool': 'literature.rag_read(version_id=rag_version_id)', 'scientific_claims_verified': False}
                    memory = Intelligence(view, {}).memory_add('global', 'shared', paper['title'] + '\n' +
                        ' '.join(identities) + '\n' + _memory_text(note), metadata)
                    self._index_source(view, entry_id, version_id, {**document, 'title': paper['title']})
                    self._reconcile_source_aliases(view, entry_id)
                    view.put('paper_rag_versions', version_id, {'id': version_id, 'entry_id': entry_id,
                        'snapshot': version_id + '.json', 'snapshot_sha256': hashlib.sha256(body).hexdigest(),
                        'created_at': now(), 'origin_run_id': run_id, 'memory_document_id': memory['document_id']})
                    old = view.get('paper_rag', entry_id, {})
                    latest = old.get('provenance') == 'external_agent_analysis' or note['created_at'] >= old.get('analysis_created_at', '')
                    view.put('paper_rag', entry_id, {'id': entry_id, 'paper': copy.deepcopy(paper) if latest else old['paper'],
                        'latest_version': version_id if latest else old['latest_version'],
                        'versions': sorted(set(old.get('versions', [])) | {version_id}),
                        'created_at': old.get('created_at', now()), 'updated_at': now(),
                        'analysis_created_at': note['created_at'] if latest else old['analysis_created_at'],
                        'provenance': 'shared_paper_rag'})
                    view.put('paper_rag_jobs', job_id, {**job, 'status': 'completed', 'version_id': version_id})
                result['published'] += 1
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                # The committed task note is never reported as a failed note save.
                self.store.put('paper_rag_jobs', job_id, {**job, 'status': 'pending', 'error_type': type(exc).__name__})
                result['pending'] += 1
        return result

    def search(self, query, limit=12, *, sort='relevance', year_from=None, year_to=None, date_from=None, date_to=None, ccf_ab_only=False):
        subject, aspects = search_intent(query)
        query_terms = _features(subject)
        if not query_terms:
            if not aspects:
                return []
            # A field-only question browses existing analyses locally. The agent
            # supplies the current paper/topic when narrowing this evidence.
            scores = {entry['latest_version']: 1.0 for entry in self.store.list('paper_rag')}
        else:
            scores = {}
        qvec = _vector(query_terms)
        for chunk in self.store.memory_candidates(query_terms, 'global') if query_terms else []:
            meta = chunk.get('metadata', {})
            if meta.get('kind') == 'paper_rag_metadata' and meta.get('metadata_id') != self.store.get(
                    'paper_rag_metadata', meta.get('rag_entry_id'), {}).get('id'):
                continue  # Superseded labels remain auditable, but cannot produce search hits.
            version = meta.get('rag_version_id')
            if not version:
                continue
            terms = _features(chunk['text'])
            overlap = sum(min(n, terms.get(w, 0)) for w, n in query_terms.items()) / sum(query_terms.values())
            if overlap < .45:
                continue
            vector = _vector(terms)
            cosine = sum(v * vector.get(w, 0) for w, v in qvec.items())
            source = self.store.get('paper_rag_sources', meta.get('rag_source_id'), {}) if meta.get('rag_source_id') else {}
            for candidate in source.get('versions', [version]):
                scores[candidate] = max(scores.get(candidate, 0), .85 * overlap + .15 * max(cosine, 0))
        found = {}
        for version, score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
            try:
                payload = self.load(version)
            except (ValueError, OSError, KeyError, TypeError):
                continue
            from .paper_rag_metadata import paper_view
            entry = self.canonical_id(payload['entry_id'])
            paper = paper_view(self, payload['paper'], entry)
            from .literature_relevance import domain_match
            if not domain_match(query, paper):
                continue
            if ccf_ab_only:
                from .venue_policy import assess
                if not assess(paper)['eligible']:
                    continue
            published, year = paper.get('publication_date'), paper.get('year')
            if published and published > date.today().isoformat():
                continue
            if (date_from or date_to) and (not published or not (date_from or '1800-01-01') <= published <= (date_to or date.today().isoformat())):
                continue
            if (year_from or year_to) and (type(year) is not int or not (year_from or 1800) <= year <= (year_to or date.today().year)):
                continue
            if entry in found:
                continue
            found[entry] = {**copy.deepcopy(paper), 'rag_entry_id': entry, 'canonical_paper_id': entry, 'rag_version_id': version,
                'rag_score': round(score, 5), 'rag_analysis_at': payload['analysis_created_at'],
                **({'rag_subject_query': subject, 'rag_matched_aspects': aspects,
                    'rag_evidence': aspect_evidence(payload, aspects)} if aspects else {}),
                'provenance': 'shared_paper_rag', 'retrieval_origin': 'local_rag',
                'scope': '历史任务分析与原文可复用；不是本次联网发现或本次AI新阅读。'}
        values = list(found.values())
        self._coverage_hints(values)
        if sort == 'recent':
            values.sort(key=lambda p: p.get('publication_date') or '', reverse=True)
        return values if limit is None else values[:max(1, min(limit, 50))]

    def _coverage_hints(self, papers):
        counts = {}
        for review in self.store.list('paper_rag_curation'):
            entry_id = self.canonical_id(review['entry_id'])
            counts[entry_id] = counts.get(entry_id, 0) + 1
        for paper in papers:
            entry_id = paper['rag_entry_id']
            paper['rag_manual_supplement_count'] = counts.get(entry_id, 0)
            paper['rag_analysis_version_count'] = len(self.store.get('paper_rag', entry_id, {}).get('versions', []))
            paper['canonical_paper_id'] = entry_id
            paper['rag_source_version_count'] = sum(self.canonical_id(s['entry_id']) == entry_id
                and not self.store.get('paper_rag_source_redirects', s['id']) for s in self.store.list('paper_rag_sources'))
            paper['rag_identifiers'] = self.store.get('paper_identities', entry_id, {}).get('identifiers', aliases(paper))
            if counts.get(entry_id):
                paper['rag_read_hint'] = 'analysis包含用户授权的逐篇补录及历史版本；按sources索引分页读原文，补录不等于复现或本任务新阅读。'

    @staticmethod
    def query_key(query, options):
        return digest([query.strip().casefold(), options])

    def query_fresh(self, query, options):
        checked = self.store.get('paper_rag_queries', self.query_key(query, options), {}).get('checked_at')
        return bool(checked and datetime.fromisoformat(checked).astimezone().date() == date.today())

    def record_query(self, query, options, report):
        if report['diagnostics'].get('errors'):
            return
        key = self.query_key(query, options)
        self.store.put('paper_rag_queries', key, {'id': key, 'query': query, 'options': options,
            'checked_at': report['diagnostics'].get('checked_at', now())})

    def read(self, version_id, *, section='analysis', offset=0, limit=6000, expected_sha256=None):
        from .evidence_tools import SourceEvidenceService
        SourceEvidenceService._page_args(offset, limit, expected_sha256)
        payload = self.load(version_id)
        from .paper_rag_curation import analysis_bundle, source_text
        if section.startswith('curation_source:'):
            content = source_text(self, payload['entry_id'], section.split(':', 1)[1])
        elif section == 'source':
            content = payload['document']['text']
        elif section == 'analysis':
            content = json.dumps(analysis_bundle(self, payload), ensure_ascii=False, indent=2)
        else:
            raise ValueError('RAG section must be analysis, source or curation_source:<text_sha256>')
        sha = hashlib.sha256(content.encode()).hexdigest()
        if expected_sha256 and sha != expected_sha256:
            raise ValueError('Shared paper page hash mismatch')
        if offset > len(content):
            raise ValueError('Offset exceeds shared paper length')
        end = min(offset + limit, len(content))
        return {'rag_version_id': version_id, 'canonical_paper_id': self.canonical_id(payload['entry_id']),
            'section': section, 'text': content[offset:end],
            'sha256': sha, 'offset': offset, 'total_chars': len(content),
            'next_offset': end if end < len(content) else None, 'untrusted': True,
            'provenance': 'shared_paper_rag', 'scientific_claims_verified': False}

    def adopt(self, engine, intelligence, run_id, paper, *, require_fulltext=False):
        """Copy immutable evidence into this task, retaining historical attribution."""
        from .literature_reading import _save_link, _run_papers, has_fulltext
        engine.check_active(run_id)
        from . import venue_policy
        from .reference_papers import is_uploaded_paper
        if (venue_policy.enabled(self.store.get('runs', run_id)) and not is_uploaded_paper(self.store, run_id, paper)
                and not venue_policy.assess(paper)['eligible']):
            return None
        if paper['id'] not in _run_papers(self.store, run_id):
            raise ValueError('Shared paper must first be registered in this research')
        version_id = paper.get('rag_version_id')
        if not version_id:
            entry_id = resolve(self.store, paper)
            if entry_id:
                version_id = self.store.get('paper_rag', entry_id, {}).get('latest_version')
        if not version_id:
            return None
        payload = self.load(version_id)
        def complete_uploaded_analysis(value):
            return (value.get('paper', {}).get('identity_kind') != 'uploaded_document'
                    or value.get('uploaded_analysis_complete') is True)
        if not complete_uploaded_analysis(payload):
            versions = self.store.get('paper_rag', self.canonical_id(payload['entry_id']), {}).get('versions', [])
            for candidate in reversed(versions):
                if candidate == version_id:
                    continue
                try:
                    other = self.load(candidate)
                except (ValueError, OSError, KeyError, TypeError):
                    continue
                if complete_uploaded_analysis(other):
                    version_id, payload = candidate, other
                    break
            else:
                return None  # A legacy partial upload must finish its selected original.
        if require_fulltext and not has_fulltext(payload['document']):
            # A newer abstract analysis must not hide an older intact original.
            versions = self.store.get('paper_rag', self.canonical_id(payload['entry_id']), {}).get('versions', [])
            for candidate in reversed(versions):
                if candidate == version_id:
                    continue
                try:
                    other = self.load(candidate)
                except (ValueError, OSError, KeyError, TypeError):
                    continue
                if complete_uploaded_analysis(other) and has_fulltext(other['document']):
                    version_id, payload = candidate, other
                    break
            else:
                return None  # Keep partial notes; resolve only the missing body.
        if not same_paper(self.store, paper, payload['paper']):
            raise ValueError('Shared paper identity does not match requested paper')
        document = copy.deepcopy(payload['document'])
        if payload['original']:
            path = self._path(payload['original'])
            if path.stat().st_size > 12 * 1024 * 1024:
                raise ValueError('Shared paper original exceeds bound')
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != document['content_sha256']:
                raise ValueError('Shared paper original hash mismatch')
            document['_content_bytes'] = body
        source_id = engine.sources._persist(run_id, document, origin={
            'original_url': document.get('original_url') or document.get('url'),
            'rag_version_id': version_id, 'rag_entry_id': self.canonical_id(payload['entry_id'])})
        current = engine.sources.document(run_id, source_id)
        manifest = self.index_source(payload['entry_id'], version_id, payload['document'])
        reading = {**{k: current.get(k) for k in ('url', 'format', 'content_sha256', 'text_sha256', 'pages_total',
            'pages_extracted', 'original_artifact_id', 'original_bytes_available')},
            'paper_id': paper['id'], 'source_id': source_id, 'artifact_id': source_id,
            'memory_document_id': manifest['memory_document_id'], 'index_status': manifest['index_status'],
            'retrieval_status': 'pdf_text_indexed' if current.get('format') == 'pdf' else 'web_text_indexed',
            'identity_verified': True,
            'paper_metadata_sha256': hashlib.sha256(json.dumps(paper, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'rag_version_id': version_id, 'canonical_paper_id': self.canonical_id(payload['entry_id']),
            'canonical_source_id': manifest['source_id'], 'provenance': 'shared_paper_rag', 'ai_read': False,
            'scientific_claims_verified': False, 'document_identity_verified': False,
            'note': '原文和分析来自共享RAG；未执行网络下载，未宣称本次AI重新阅读。'}
        if is_uploaded_paper(self.store, run_id, paper):
            reading.update(identity_kind='uploaded_document', content_integrity_verified=True,
                bibliographic_identity_verified=paper.get('bibliographic_identity_verified') is True)
            if paper.get('uploaded_source_id'):
                original = engine.sources.document(run_id, paper['uploaded_source_id'])
                if original.get('content_sha256') != paper.get('uploaded_content_sha256'):
                    raise ValueError('Selected uploaded original hash changed during shared reuse')
                reading.update(uploaded_original_source_id=paper['uploaded_source_id'],
                    uploaded_original_content_sha256=original['content_sha256'],
                    uploaded_original_text_sha256=original['text_sha256'],
                    uploaded_original_format=original.get('format'),
                    uploaded_original_artifact_id=original.get('original_artifact_id'))
        _save_link(self.store, run_id, paper, reading)
        key = digest([run_id, paper['id'], source_id, version_id])
        self.store.mutate('paper_rag_reuses', key, lambda old: old or {'id': key, 'run_id': run_id, 'paper_id': paper['id'],
            'source_id': source_id, 'version_id': version_id, 'created_at': now()})
        return reading

    def reused_notes(self, run_id, document_loader):
        from .literature_notes import NOTE_FIELDS
        result = []
        for link in self.store.list('paper_rag_reuses', run_id):
            try:
                payload = self.load(link['version_id'])
                if payload.get('provenance') == 'external_agent_analysis':
                    continue  # Imported analysis is searchable, but never grants task reading credit.
                source = document_loader(run_id, link['source_id'])
                evidence = self.store.get('run_evidence', f'{run_id}:{link["paper_id"]}', {})
                if (source['text_sha256'] != payload['note']['text_sha256']
                        or source.get('rag_version_id') != link['version_id']
                        or not same_paper(self.store, evidence['paper'], payload['paper'])):
                    continue
                old = payload['note']
                note = {key: copy.deepcopy(old[key]) for key in NOTE_FIELDS}
                note.update(paper_id=link['paper_id'], source_id=link['source_id'])
                for field in ('excerpts', 'results'):
                    for item in note[field]:
                        item['locator'] = item['locator'].replace(old['source_id'] + '#', link['source_id'] + '#', 1)
                note.update(id='rag-note:' + link['id'], run_id=run_id, role='scout',
                    note_sha256=digest(note), created_at=link['created_at'], generation='reused_ai_extraction',
                    reading_scope='inspected_passages', scientific_claims_verified=False,
                    document_identity_verified=False, paper_identity_verified=True, source_untrusted=True,
                    content_sha256=source.get('content_sha256'), source_text_truncated=source.get('text_truncated', False),
                    retrieval_status=evidence['reading']['retrieval_status'], source_read_event_ids=[],
                    source_read_receipts=[], citations=[], canonical_paper_id=self.canonical_id(payload['entry_id']),
                    reused_from={'rag_entry_id': self.canonical_id(payload['entry_id']),
                        'rag_version_id': link['version_id'], 'run_id': payload['origin_run_id'],
                        'note_id': payload['origin_note_id'], 'analysis_created_at': payload['analysis_created_at']},
                    scope_note='复用历史任务的已验证片段笔记；保留原分析日期与来源。本次未重新阅读全文，原任务相关性需结合当前目标判断。')
                current_paper = evidence['paper']
                if (current_paper.get('identity_kind') == 'uploaded_document'
                        or payload['paper'].get('identity_kind') == 'uploaded_document'):
                    bibliographic_verified = current_paper.get('bibliographic_identity_verified')
                    if bibliographic_verified is None:
                        bibliographic_verified = (payload['paper'].get('verified') is True
                            and payload['paper'].get('verification', {}).get('status') == 'verified')
                    note.update(identity_kind='uploaded_document', content_integrity_verified=True,
                        bibliographic_identity_verified=bibliographic_verified is True,
                        identity_scope='保存原文的字节身份；不表示发表身份或科学结论已核验。')
                if old.get('ocr_performed'):
                    note.update(ocr_performed=True, source_transcription='machine_ocr',
                        ocr_warning=old.get('ocr_warning'),
                        original_ocr_image=copy.deepcopy(old.get('ocr_image')))
                result.append(note)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return result

    def catalog(self, query='', **filters):
        from .paper_rag_catalog import browse
        return browse(self, query, **filters)

    def original_pdf(self, version_id):
        payload = self.load(version_id)
        original = payload.get('original')
        if not original or payload['document'].get('format') != 'pdf':
            raise ValueError('Shared PDF is not available')
        path = self._path(original)
        if not path.is_file() or path.stat().st_size > 12 * 1024 * 1024:
            raise ValueError('Shared PDF is unavailable or exceeds bound')
        body = path.read_bytes()
        if not body.startswith(b'%PDF-') or hashlib.sha256(body).hexdigest() != payload['document']['content_sha256']:
            raise ValueError('Shared PDF integrity check failed')
        return path
