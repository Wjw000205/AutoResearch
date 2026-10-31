"""One bibliographic identity, with immutable evidence versions beneath it.

Task IDs and provider IDs are aliases. A merge changes only the registry and
its redirects; it never changes a saved paper, paid input, source or analysis.
Titles alone are not paper identities.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from urllib.parse import unquote

from .storage import now


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _doi(value):
    text = unquote(str(value or '')).strip().casefold()
    text = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', text)
    return text if re.fullmatch(r'10\.\d{4,9}/\S+', text) else None


def _arxiv(value):
    text = str(value or '').strip().casefold()
    text = re.sub(r'^(?:https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/|arxiv:)', '', text)
    text = text.removesuffix('.pdf')
    return re.sub(r'v\d+$', '', text) if re.fullmatch(
        r'(?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?', text) else None


def aliases(paper):
    """Normalize explicit identifiers, including DOI/provider ID spellings."""
    identifier = str(paper.get('id') or '').strip()
    values = {'id:' + identifier.casefold()} if identifier else set()
    doi = _doi(paper.get('doi')) or _doi(identifier)
    arxiv = _arxiv(paper.get('arxiv_id'))
    if not arxiv and (identifier.casefold().startswith('arxiv:') or 'arxiv.org/' in identifier.casefold()):
        arxiv = _arxiv(identifier)
    if doi:
        values.add('doi:' + doi)
    if arxiv:
        values.add('arxiv:' + arxiv)
    if paper.get('dblp_key'):
        values.add('dblp:' + str(paper['dblp_key']).strip())
    for key, prefix in (('pmid', 'pmid:'), ('pmcid', 'pmcid:'),
                        ('semantic_scholar_id', 's2:'), ('openalex_id', 'openalex:')):
        if paper.get(key):
            values.add(prefix + str(paper[key]).strip().casefold())
    return sorted(values)


def _title(paper):
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', str(paper.get('title') or '')).casefold())


class IdentityConflict(ValueError):
    pass


def canonical(store, entry_id):
    seen = set()
    while entry_id:
        if entry_id in seen:
            raise IdentityConflict('Paper identity redirect cycle')
        seen.add(entry_id)
        redirect = store.get('paper_rag_redirects', entry_id)
        if not redirect:
            return entry_id
        if redirect.get('id') != entry_id or not redirect.get('canonical_id'):
            raise IdentityConflict('Invalid paper identity redirect')
        entry_id = redirect['canonical_id']
    return None


def resolve(store, paper):
    roots = {canonical(store, row['entry_id']) for alias in aliases(paper)
             if (row := store.get('paper_rag_aliases', digest(alias)))}
    if len(roots) > 1:
        raise IdentityConflict('Paper aliases need registry reconciliation')
    if roots:
        return next(iter(roots))
    return None


def same_paper(store, left, right):
    a, b = set(aliases(left)), set(aliases(right))
    for prefix in ('doi:', 'arxiv:', 'dblp:', 'pmid:', 'pmcid:'):
        x, y = {v for v in a if v.startswith(prefix)}, {v for v in b if v.startswith(prefix)}
        if x and y and not x & y:
            return False
    if a & b:
        return True
    try:
        x, y = resolve(store, left), resolve(store, right)
    except IdentityConflict:
        return False
    return bool(x and x == y)


class Transaction:
    """The same SQLite write transaction for identity, versions and indexing."""
    def __init__(self, db):
        self.db = db

    def get(self, collection, key, default=None):
        row = self.db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
        return json.loads(row[0]) if row else default

    def list(self, collection, run_id=None):
        sql, args = 'SELECT data FROM records WHERE collection=?', [collection]
        if run_id is not None:
            sql += " AND json_extract(data,'$.run_id')=?"
            args.append(run_id)
        return [json.loads(row[0]) for row in self.db.execute(sql + ' ORDER BY updated_at,key', args)]

    def put(self, collection, key, value):
        self.db.execute('INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(collection,key) '
            'DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
            (collection, key, json.dumps(value, ensure_ascii=False, allow_nan=False), now()))
        return value

    def delete(self, collection, key):
        self.db.execute('DELETE FROM records WHERE collection=? AND key=?', (collection, key))

    def batch(self, puts=(), deletes=()):
        for collection, key in deletes:
            self.delete(collection, key)
        for collection, key, value in puts:
            self.put(collection, key, value)


def register_in(store, paper):
    values = aliases(paper)
    if not values:
        raise IdentityConflict('Paper has no explicit identifier')
    roots = {canonical(store, row['entry_id']) for alias in values
             if (row := store.get('paper_rag_aliases', digest(alias)))}
    members = {key: store.get('paper_identities', key) or store.get('paper_rag', key) for key in roots}
    if any(not value for value in members.values()):
        raise IdentityConflict('Paper alias points to a missing identity')
    combined = set(values)
    for member in members.values():
        combined.update(member.get('identifiers', aliases(member['paper'])))
    for prefix in ('doi:', 'arxiv:', 'dblp:', 'pmid:', 'pmcid:'):
        if len({value for value in combined if value.startswith(prefix)}) > 1:
            raise IdentityConflict('Conflicting explicit paper identifiers: ' + prefix)
    if len(roots) > 1:
        # A provider's verified cross-identifiers can bridge two old entries.
        # Unverified submissions need agreement on title, year and authors.
        trusted = paper.get('verified') is True and (paper.get('verification') or {}).get('status') == 'verified'
        authors = {str(a).strip().casefold() for a in paper.get('authors', [])}
        if not trusted and any(not _title(paper) or _title(paper) != _title(m['paper'])
                or not paper.get('year') or paper.get('year') != m['paper'].get('year')
                or not authors or not authors & {str(a).strip().casefold() for a in m['paper'].get('authors', [])}
                for m in members.values()):
            raise IdentityConflict('Paper identity bridge lacks bibliographic agreement')
    entries = {key: store.get('paper_rag', key) for key in roots}
    # Keep an existing published ID stable. A later DOI adds an alias rather
    # than renaming the record and invalidating immutable version hashes.
    ranked = sorted(roots, key=lambda key: (not bool(entries[key]), members[key].get('created_at', ''), key))
    preferred = next((v for prefix in ('doi:', 'arxiv:', 'dblp:', 'pmid:', 'pmcid:', 's2:', 'openalex:', 'id:')
                      for v in values if v.startswith(prefix)), values[0])
    entry_id = ranked[0] if ranked else digest(preferred)
    timestamp = now()
    old = members.get(entry_id) or {}
    selected = old.get('paper') or copy.deepcopy(paper)
    if paper.get('verified') is True or not selected.get('verified'):
        selected = copy.deepcopy(paper)
    identity = {'id': entry_id, 'paper': selected, 'identifiers': sorted(combined),
                'created_at': old.get('created_at', timestamp), 'updated_at': timestamp,
                'policy_version': 1, 'scientific_claims_verified': False}
    store.put('paper_identities', entry_id, identity)
    active = [entry for entry in entries.values() if entry]
    if active:
        # A verified local default wins over an external submission.
        best = max(active, key=lambda e: (e.get('provenance') != 'external_agent_analysis',
                                         e.get('analysis_created_at', ''), e['id']))
        store.put('paper_rag', entry_id, {**best, 'id': entry_id,
            'versions': sorted({v for entry in active for v in entry.get('versions', [])}),
            'created_at': min(e.get('created_at', timestamp) for e in active),
            'updated_at': timestamp})
    if len(roots) > 1:
        audit = {'canonical_id': entry_id, 'identifiers': sorted(combined),
                 'previous_identities': members, 'previous_entries': entries,
                 'basis': 'verified_cross_identifiers' if trusted else 'bibliographic_agreement',
                 'scientific_claims_verified': False}
        audit_id = digest(audit)
        store.put('paper_identity_merges', audit_id, {'id': audit_id, **audit, 'created_at': timestamp})
    for key in roots - {entry_id}:
        store.put('paper_rag_redirects', key, {'id': key, 'canonical_id': entry_id, 'created_at': timestamp})
        store.delete('paper_rag', key)
        store.delete('paper_identities', key)
    for alias in combined:
        store.put('paper_rag_aliases', digest(alias), {'id': digest(alias), 'alias': alias, 'entry_id': entry_id})
    return entry_id


def register(store, paper):
    with store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        return register_in(Transaction(db), paper)


def task_paper(store, run_id, identifier, papers=None):
    """An alias may resolve only to a paper already verified in this task."""
    owned = papers is None
    if papers is None:
        from .literature_reading import _paper_catalogue
        papers = _paper_catalogue(store, run_id)
    if identifier in papers:
        return copy.deepcopy(papers[identifier]) if owned else papers[identifier]
    target = str(identifier).removeprefix('paper:')
    wanted = set(aliases({'id': identifier}))
    roots = {canonical(store, row['entry_id']) for alias in wanted
             if (row := store.get('paper_rag_aliases', digest(alias)))}
    candidates = []
    for paper in papers.values():
        values = set(aliases(paper))
        resolved = resolve(store, paper)
        if wanted & values or resolved == target or resolved in roots:
            candidates.append(paper)
    if candidates and all(same_paper(store, candidates[0], p) for p in candidates[1:]):
        return copy.deepcopy(candidates[0]) if owned else candidates[0]
    return None


def task_links(store, run_id, paper):
    return [link for link in store.list('run_evidence', run_id)
            if link.get('run_id') == run_id and isinstance(link.get('paper'), dict)
            and same_paper(store, paper, link['paper'])]


def noted_papers(store, run_id, notes, papers=None):
    """All owned paper aliases covered by qualified notes, without new notes."""
    if papers is None:
        from .literature_reading import _run_papers
        papers = _run_papers(store, run_id)
    originals = [p for note in notes if (p := task_paper(store, run_id, note['paper_id'], papers))]
    return {identifier for identifier, paper in papers.items() if any(same_paper(store, paper, p) for p in originals)}
