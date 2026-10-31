"""Small transactional JSON store. SQLite is the source of truth, never process memory."""
from __future__ import annotations

import json
import copy
import re
import sqlite3
import sys
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


TASK_COLLECTIONS = frozenset({
    'experiment_decisions', 'experiment_decision_sessions', 'experiment_evidence', 'experiment_notes', 'artifact_evidence_successors', 'architecture_prompt_successors',
    'experiment_sources', 'experiment_analyses', 'experiment_cleanups', 'performance_policies', 'performance_policy_archives', 'saved_runtime_audits',
    'rapid_experiment_revisions', 'experiment_memories', 'rapid_continuations', 'rapid_continuation_archives',
    'experiment_protocols', 'experiment_original_receipts', 'optimization_loops',
    'training_adjustments',
    'training_review_recoveries', 'tuning_progress_recoveries', 'yaml_review_successors',
    'dataset_assets', 'dataset_downloads', 'dataset_transitions', 'dataset_transition_routes',
    'comparison_protocol_repairs',
    'code_layout_relocations', 'codex_acceptance_replays', 'code_repair_packets',
    'implementation_source_supplements',
    'implementation_acceptance_budgets', 'implementation_review_clock_adjustments',
    'steps', 'cycle_steps', 'research_cycles', 'revision_plans', 'consensus',
    'proposal_reviews', 'debate_inputs', 'innovation_sessions', 'innovation_attempts',
    'innovation_readings', 'innovation_material_archives', 'handoffs', 'handoff_inputs',
    'handoff_progress', 'decisions', 'operator_resumes', 'run_control_requests', 'programs', 'selections',
    'tuning_plans', 'experiment_plans', 'protocol_reviews', 'implementation_routing_recoveries', 'experiment_restarts', 'experiments',
    'codex_workflows', 'implementation_document_drafts', 'architecture_diagrams', 'codex_checks', 'codex_check_attempts', 'experiment_attempts', 'artifacts', 'stage_contexts', 'literature_inputs', 'literature_notes',
    'run_evidence', 'paper_rag_reuses', 'paper_source_bindings', 'pdf_citations', 'graph_status', 'contexts', 'context_snapshots', 'transcripts',
    'memory', 'memory_documents', 'response_cache', 'request_audits', 'moderation_reviews', 'codex_reasoning', 'context_evidence', 'paper_dossiers',
    'semantic_vectors', 'paper_ocr', 'paper_visual', 'research_states', 'research_state_sources',
    'context_receipts', 'context_commits', 'context_documents', 'context_manifests', 'memory_trees', 'evidence_deliveries',
    'execution_archives', 'tool_invocations', 'tool_tasks', 'architecture_reviews', 'architecture_revisions', 'architecture_definitions', 'architecture_progress',
})
SHARED_COLLECTIONS = frozenset({
    'settings', 'circuits', 'literature', 'literature_status', 'tool_cache',
    'references', 'runtime', 'run_deletions', 'paper_rag', 'paper_rag_versions',
    'paper_rag_aliases', 'paper_rag_jobs', 'paper_rag_queries', 'paper_rag_curation',
    'paper_rag_imports', 'paper_rag_metadata', 'paper_identities', 'paper_rag_redirects',
    'paper_identity_merges', 'paper_rag_sources', 'paper_rag_source_redirects', 'paper_identity_migrations',
})


class RunDeletedError(ValueError):
    """A durable deletion marker prevents a stale writer from reviving a run."""


def _run_owner(collection: str, key: str, value: Any) -> str | None:
    if collection == 'runs':
        return key
    if collection in SHARED_COLLECTIONS:
        return None
    if isinstance(value, dict) and value.get('run_id'):
        return value['run_id']
    # Legacy task records sometimes used only run_id:stage/role as their key.
    if collection in TASK_COLLECTIONS or collection == 'budgets':
        return key.split(':', 1)[0]
    return None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return uuid.uuid4().hex


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: '[redacted]' if k.lower() in {'api_key', 'apikey', 'authorization', 'x-api-key'} else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'sk-[A-Za-z0-9_-]{12,}', '[redacted]', value)
    return value


_MANIFEST_HANDOFF = "coalesce(json_extract(data,'$.handoff_id'),json_extract(data,'$.operation_id'))"
_DELIVERY_HANDOFF = ("CASE WHEN json_type(data,'$.handoff_id') IS NULL "
                     "THEN json_extract(data,'$.operation_id') ELSE json_extract(data,'$.handoff_id') END")


def _migrate_delivery_lookup(db):
    """Select exact receipt scopes without decoding every historical delivery.

    An explicit null handoff is not a legacy missing handoff. Keep that
    distinction in the index; original receipts still undergo all SHA checks.
    """
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    for identity in ('document_id', 'source_id'):
        db.execute('CREATE INDEX IF NOT EXISTS evidence_delivery_' + identity + '_v1 ON records('
            "json_extract(data,'$.run_id'),json_extract(data,'$.role'),"
            "json_extract(data,'$." + identity + "'),(" + _DELIVERY_HANDOFF + '),updated_at,key) '
            "WHERE collection='evidence_deliveries'")
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES (?,?,?)', (
        'evidence-delivery-lookup-v1', now(),
        'Additive exact run/role/source/handoff receipt indexes; original evidence retained'))


def _migrate_manifest_lookup(db):
    """Add metadata-only selectors without rebuilding or rewriting records.

    COALESCE deliberately retains the existing tracking-policy lookup: absent
    and explicit JSON-null handoff_id may locate operation_id candidates. The
    evidence reader still checks their original Python ownership and receipts.
    """
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    for name, fields in (
        ('context_manifest_handoff_v1', _MANIFEST_HANDOFF),
        ('context_manifest_request_v1', "json_extract(data,'$.request_id')"),
        ('context_manifest_role_v1', "json_extract(data,'$.role')"),
    ):
        db.execute('CREATE INDEX IF NOT EXISTS ' + name + ' ON records('
            "json_extract(data,'$.run_id')," + fields + ',updated_at,key) '
            "WHERE collection='context_manifests'")
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES (?,?,?)', (
        'evidence-manifest-lookup-v1', now(),
        'Additive context manifest run/handoff, run/request and run/role indexes; '
        'original manifests and delivery policies retained'))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS records (
                    collection TEXT NOT NULL, key TEXT NOT NULL,
                    data TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(collection,key));
                CREATE INDEX IF NOT EXISTS records_run
                    ON records(collection,json_extract(data,'$.run_id'),updated_at,key);
                CREATE INDEX IF NOT EXISTS records_recent
                    ON records(collection,updated_at,key);
                CREATE INDEX IF NOT EXISTS codex_reasoning_list_metadata_v2
                    ON records(json_extract(data,'$.run_id'),+json_extract(data,'$.created_at') DESC,
                        updated_at,key,+json_extract(data,'$.selection.purpose'),+json_extract(data,'$.role'))
                    WHERE collection='codex_reasoning';
                DROP INDEX IF EXISTS codex_reasoning_list_metadata;
                CREATE INDEX IF NOT EXISTS transcript_output_request
                    ON records(json_extract(data,'$.request_id'),json_extract(data,'$.run_id'))
                    WHERE collection='transcripts' AND json_extract(data,'$.kind')='output';
                CREATE INDEX IF NOT EXISTS transcript_output_operation
                    ON records(json_extract(data,'$.operation_id'),json_extract(data,'$.run_id'))
                    WHERE collection='transcripts' AND json_extract(data,'$.kind')='output';
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, kind TEXT NOT NULL,
                    message TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
                CREATE INDEX IF NOT EXISTS events_handoff_tool
                    ON events(run_id,json_extract(data,'$.handoff_id'),id DESC)
                    WHERE kind IN ('tool.started','tool.completed','tool.failed');
                CREATE TABLE IF NOT EXISTS memory_terms (
                    term TEXT NOT NULL, memory_id TEXT NOT NULL,
                    PRIMARY KEY(term,memory_id));
                CREATE INDEX IF NOT EXISTS memory_terms_document ON memory_terms(memory_id);
                CREATE TRIGGER IF NOT EXISTS memory_terms_insert AFTER INSERT ON records
                WHEN new.collection='memory' BEGIN
                    INSERT OR IGNORE INTO memory_terms(term,memory_id)
                    SELECT key,new.key FROM json_each(new.data,'$.features');
                END;
                CREATE TRIGGER IF NOT EXISTS memory_terms_update AFTER UPDATE ON records
                WHEN new.collection='memory' BEGIN
                    DELETE FROM memory_terms WHERE memory_id=old.key;
                    INSERT OR IGNORE INTO memory_terms(term,memory_id)
                    SELECT key,new.key FROM json_each(new.data,'$.features');
                END;
                CREATE TRIGGER IF NOT EXISTS memory_terms_delete AFTER DELETE ON records
                WHEN old.collection='memory' BEGIN
                    DELETE FROM memory_terms WHERE memory_id=old.key;
                END;
            ''')
            from .reliability_migrations import migrate
            migrate(db)
            _migrate_manifest_lookup(db)
            _migrate_delivery_lookup(db)

    @contextmanager
    def _db(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA busy_timeout=30000')
            db.execute('PRAGMA foreign_keys=ON')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def get(self, collection: str, key: str, default=None):
        with self._read_db() as db:
            row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
        return json.loads(row['data']) if row else default

    def implementation_workflow_design(self, key):
        """Read the design identity without decoding every historical delivery."""
        with self._read_db() as db:
            row=db.execute("SELECT json_extract(data,'$.id') AS id, "
                "json_extract(data,'$.run_id') AS run_id, json_extract(data,'$.common.design') AS design "
                "FROM records WHERE collection='codex_workflows' AND key=?",(key,)).fetchone()
        if row is None:return {}
        return {'id':row['id'],'run_id':row['run_id'],
                'common':{'design':json.loads(row['design']) if row['design'] is not None else None}}

    @contextmanager
    def _read_db(self):
        """Independent read-only connection; slow reads never hold the writer lock."""
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA query_only=ON')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def read_snapshot(self):
        """A consistent, scoped, read-only view; decoded records live only here."""
        with self._read_db() as db:
            db.execute('BEGIN')
            view = _ReadSnapshot(self, db)
            try:
                yield view
            finally:
                view._closed = True
                view._clear_cache()

    def put(self, collection: str, key: str, value: Any):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._guard_write(db, collection, key, value)
            db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(collection,key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                       (collection, key, json.dumps(value, ensure_ascii=False, allow_nan=False), now()))
        return value

    def list(self, collection: str, run_id: str | None = None):
        where, parameters = self._record_filter(collection, run_id)
        with self._read_db() as db:
            # Do not retain every raw JSON string alongside its decoded tree.
            return [json.loads(row['data']) for row in db.execute(
                'SELECT data FROM records WHERE ' + where + ' ORDER BY updated_at,key', parameters)]

    def record_keys(self, collection: str, run_id: str | None = None, *,
                    predicate: str = '1', parameters=(), limit: int | None = None):
        """Internal metadata selection; original records still need exact checks.

        Predicates are static SQL from trusted callers, never model or API input.
        Select keys before decoding large immutable inputs. Scoped selections use
        the collection/run index and retain list()'s stable ordering.
        """
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError('Record selection limit must be a non-negative integer')
        where, scope = self._record_filter(collection, run_id)
        index = ' INDEXED BY records_run' if run_id is not None else ''
        if collection == 'context_manifests' and run_id is not None:
            # These static internal selectors avoid parsing every large run
            # manifest for each historical handoff. Other collections/predicates
            # retain the generic index and original stable result ordering.
            if _MANIFEST_HANDOFF in predicate:
                index = ' INDEXED BY context_manifest_handoff_v1'
            elif "json_extract(data,'$.request_id')" in predicate:
                index = ' INDEXED BY context_manifest_request_v1'
            elif "json_extract(data,'$.role')" in predicate:
                index = ' INDEXED BY context_manifest_role_v1'
            if index != ' INDEXED BY records_run':
                # SQLite must see the partial index condition as a literal;
                # the bound generic collection filter alone cannot prove it.
                where += " AND collection='context_manifests'"
        if collection == 'evidence_deliveries' and run_id is not None and "json_extract(data,'$.role')" in predicate:
            for identity in ('document_id', 'source_id'):
                if "json_extract(data,'$." + identity + "')" in predicate:
                    index = ' INDEXED BY evidence_delivery_' + identity + '_v1'
                    where += " AND collection='evidence_deliveries'"
                    break
        query = 'SELECT key FROM records' + index + ' WHERE ' + where + ' AND (' + predicate + ') ORDER BY updated_at,key'
        bound = [*scope, *parameters]
        if limit is not None:
            query += ' LIMIT ?'
            bound.append(limit)
        with self._read_db() as db:
            return [row['key'] for row in db.execute(query, bound)]

    def active_reasoning(self, run_id: str, statuses):
        """Select possible active requests, then recheck their original receipt."""
        statuses = tuple(statuses)
        if not statuses:
            return False
        placeholders = ','.join('?' for _ in statuses)
        keys = self.record_keys('codex_reasoning', run_id,
            predicate="json_extract(data,'$.checkpoint.status') IN (" + placeholders + ')'
                      " OR json_type(data,'$.checkpoint') NOT IN ('object','null')"
                      " OR json_type(data,'$.checkpoint.status') IN ('array','object')",
            parameters=statuses)
        for key in keys:
            record = self.get('codex_reasoning', key, {})
            if record.get('run_id') != run_id:
                continue
            checkpoint = record.get('checkpoint')
            # Do not let metadata filtering turn a malformed original into an
            # inactive request. These originals also blocked the old guards.
            if checkpoint is not None and not isinstance(checkpoint, dict):
                return True
            status = (checkpoint or {}).get('status')
            if isinstance(status, (list, dict)) or status in statuses:
                return True
        return False

    @staticmethod
    def _record_filter(collection: str, run_id: str | None):
        where, parameters = 'collection=?', [collection]
        if run_id is not None:
            where += " AND json_extract(data,'$.run_id')=?"
            parameters.append(run_id)
        return where, parameters

    def count(self, collection: str, run_id: str | None = None) -> int:
        """Count records without reading or decoding their scientific bodies."""
        where, parameters = self._record_filter(collection, run_id)
        with self._read_db() as db:
            return db.execute('SELECT COUNT(*) FROM records WHERE ' + where, parameters).fetchone()[0]

    def matching_input_transcripts(self, run_id: str, role: str, task: str):
        """Newest exact input candidates, without decoding unrelated transcripts."""
        where, parameters = self._record_filter('transcripts', run_id)
        where += " AND json_extract(data,'$.kind')='input' AND json_extract(data,'$.role')=? AND json_extract(data,'$.task')=?"
        with self._read_db() as db:
            rows = db.execute('SELECT data FROM records WHERE ' + where +
                              ' ORDER BY updated_at DESC,key DESC', [*parameters, role, task]).fetchall()
        return [json.loads(row['data']) for row in rows]

    def matching_output_transcript(self, run_id: str, role: str, operation_id, created_at):
        """Latest original output, in the same order as reversed(list())."""
        where, parameters = self._record_filter('transcripts', run_id)
        where += " AND json_extract(data,'$.kind')='output' AND json_extract(data,'$.role')=? AND json_extract(data,'$.operation_id') IS ?"
        with self._read_db() as db:
            rows = db.execute('SELECT data FROM records WHERE ' + where +
                              ' ORDER BY updated_at DESC,key DESC', [*parameters, role, operation_id])
            for result in rows:
                row = json.loads(result['data'])
                # Keep the legacy comparison: a missing date is '', while a
                # malformed explicit null must not become a valid empty date.
                if row.get('created_at', '') >= created_at:
                    return row
        return None

    def has_output_transcript(self, run_id: str | None, *, request_id, operation_id) -> bool:
        """Check exact output identities without loading transcript bodies.

        Preserve dict.get equality: missing IDs equal None, empty IDs are not
        missing, and JSON objects must not equal their string representations.
        """
        # Keep these literals aligned with the partial-index predicates. Bind
        # every caller-provided value; each EXISTS can then seek its own index.
        where = "collection='transcripts' AND json_extract(data,'$.kind')='output'"
        scope_parameters = []
        if run_id is not None:
            where += " AND json_extract(data,'$.run_id')=?"
            scope_parameters.append(run_id)
        ordinary = all(value is None or isinstance(value, str) for value in (request_id, operation_id))
        if ordinary:
            matches, parameters = [], []
            for field, value in (('request_id', request_id), ('operation_id', operation_id)):
                if value is None:
                    # A bound IS preserves missing/null equality while letting
                    # SQLite seek the expression index for a null identity.
                    condition = f"json_extract(data,'$.{field}') IS ?"
                    values = [None]
                else:
                    condition = f"json_type(data,'$.{field}')='text' AND json_extract(data,'$.{field}')=?"
                    values = [value]
                matches.append('EXISTS(SELECT 1 FROM records WHERE ' + where + ' AND ' + condition + ')')
                parameters.extend([*scope_parameters, *values])
            query = 'SELECT ' + ' OR '.join(matches)
        else:
            # Legacy malformed IDs can be numeric or composite. SQLite scalar
            # extraction rounds oversized integers and stringifies objects;
            # compare only the two JSON ID values in Python for exact parity.
            where += " AND _ar_output_ids_match(json_extract(data,'$.request_id','$.operation_id'))"
            query = 'SELECT EXISTS(SELECT 1 FROM records WHERE ' + where + ')'
            parameters = scope_parameters
        with self._read_db() as db:
            if not ordinary:
                def matches_ids(raw):
                    values = json.loads(raw)
                    return values[0] == request_id or values[1] == operation_id
                db.create_function('_ar_output_ids_match', 1, matches_ids, deterministic=True)
            try:
                return bool(db.execute(query, parameters).fetchone()[0])
            finally:
                if not ordinary:
                    db.create_function('_ar_output_ids_match', 1, None)

    def reasoning_request_summaries(self, run_id: str, *, limit: int = 30, offset: int = 0) -> list[dict]:
        """Read list metadata without loading frozen model inputs or replies.

        SQLite evaluates one scoped statement; full immutable requests remain
        available through get(). Decode the bounded projection after releasing
        the shared connection lock so ongoing validation can read its sources.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Request page limit must be an integer between 1 and 100')
        if type(offset) is not int or offset < 0:
            raise ValueError('Request page offset must be a non-negative integer')
        from .codex_accounting import TOKEN_FIELDS, reported_usage
        # Keep page-local counters with their exact request. Billing summaries
        # may contain totals only or a different operation page. Whitelist at
        # the SQL boundary so prompts and non-counter receipt data stay unread.
        usage_groups = []
        for group in ('last', 'total'):
            counters = []
            for field in TOKEN_FIELDS:
                path = f'$.checkpoint.usage.{group}.{field}'
                counters.append(f"'{field}',CASE WHEN json_type(data,'{path}')='integer' "
                    f"AND json_extract(data,'{path}')>=0 THEN json_extract(data,'{path}') END")
            usage_groups.append(f"'{group}',json_object({','.join(counters)})")
        usage_projection = f"json_object({','.join(usage_groups)})"
        query = f"""SELECT json_object(
            'id', json_extract(data,'$.id'),
            'role', json_extract(data,'$.role'),
            'created_at', json_extract(data,'$.created_at'),
            'selection', json_extract(data,'$.selection'),
            'status', CASE WHEN json_type(data,'$.checkpoint.status') IS NULL
                THEN 'prepared' ELSE json_extract(data,'$.checkpoint.status') END,
            'thread_id', json_extract(data,'$.checkpoint.thread_id'),
            'turn_id', json_extract(data,'$.checkpoint.turn_id'),
            'has_result', CASE json_type(data,'$.result')
                WHEN 'object' THEN EXISTS(SELECT 1 FROM json_each(data,'$.result'))
                WHEN 'array' THEN json_array_length(data,'$.result') > 0
                WHEN 'text' THEN length(json_extract(data,'$.result')) > 0
                ELSE coalesce(json_extract(data,'$.result') != 0,0) END,
            'error_code', json_extract(data,'$.checkpoint.error_code'),
            'submitted_at', json_extract(data,'$.checkpoint.turn_dispatched_at'),
            'updated_at', json_extract(data,'$.updated_at'),
            'last_event_at', json_extract(data,'$.checkpoint.last_event_at'),
            'usage', {usage_projection},
            'usage_scope', json_extract(data,'$.checkpoint.usage_scope'),
            'usage_observed_at', json_extract(data,'$.checkpoint.usage_observed_at'),
            'retry_count', (SELECT count(*) FROM json_each(data,'$.checkpoint.server_errors')
                WHERE json_type(value,'$.will_retry')='true'),
            'last_connection_event', json_extract(data,'$.checkpoint.server_errors[#-1]')
            ) AS summary FROM page JOIN records
                ON records.collection='codex_reasoning' AND records.key=page.key
            ORDER BY json_extract(records.data,'$.created_at') DESC,records.updated_at,records.key"""
        from .implementation_activity import receipt_metadata, predicate
        with self.read_snapshot() as snapshot:
            visible, args = predicate(receipt_metadata(snapshot, run_id, include_handoff_scopes=True), 'codex_reasoning')
            # The partial expression index supplies all sorting/filter fields;
            # immutable request bodies are read only after the page is frozen.
            query = ("WITH page AS MATERIALIZED (SELECT key FROM records "
                "WHERE collection='codex_reasoning' AND json_extract(data,'$.run_id')=? AND "
                + visible + " ORDER BY +json_extract(data,'$.created_at') DESC,updated_at,key LIMIT ? OFFSET ?) "
                + query)
            with snapshot._read_db() as db:
                raw = [row['summary'] for row in db.execute(query, (run_id, *args, limit, offset)).fetchall()]
        summaries = [json.loads(value) for value in raw]
        for row in summaries:
            row['has_result'] = bool(row['has_result'])
            row['usage'] = reported_usage(row['usage'])
        return summaries

    def recent_records(self, collection: str, run_id: str | None = None, *, limit: int = 10):
        """Return the last records in the same ascending order as list()."""
        if type(limit) is not int or limit < 0:
            raise ValueError('Record limit must be a non-negative integer')
        if limit == 0:
            return []
        where, parameters = self._record_filter(collection, run_id)
        with self._read_db() as db:
            rows = db.execute('SELECT data FROM records WHERE ' + where
                + ' ORDER BY updated_at DESC,key DESC LIMIT ?', [*parameters, limit]).fetchall()
        return [json.loads(row['data']) for row in reversed(rows)]

    def delete(self, collection: str, key: str):
        with self._db() as db:
            db.execute('DELETE FROM records WHERE collection=? AND key=?', (collection, key))

    def memory_needing_index(self, version: str):
        """Stream one stable migration scan while callers write WAL updates."""
        with self._read_db() as db:
            if not db.in_transaction:
                db.execute('BEGIN')
            rows = db.execute("SELECT key,data FROM records WHERE collection='memory' "
                              "AND json_extract(data,'$.index_version') IS NOT ?", (version,))
            for row in rows:
                yield row['key'], json.loads(row['data'])

    def memory_candidates(self, terms, run_id=None, *, role=None, scope=None):
        """Use the durable inverted index, applying visibility before loading text."""
        parameters = []
        query = "SELECT records.data FROM records WHERE records.collection='memory'"
        if terms:
            query += (" AND records.key IN (SELECT t.memory_id FROM memory_terms t "
                      "JOIN json_each(?) q ON t.term=q.value)")
            parameters.append(json.dumps(list(terms), ensure_ascii=False))
        from .memory_retrieval import visibility
        visible, arguments = visibility(self, run_id, role, scope)
        # Keep the canonical table name for correlated version checks.
        query += ' AND ' + visible
        parameters.extend(arguments)
        with self._read_db() as db:
            rows = db.execute(query + ' ORDER BY records.updated_at,records.key', parameters).fetchall()
        return [json.loads(row['data']) for row in rows]

    def memory_index_stats(self, run_id=None):
        query = ("SELECT COUNT(*) AS chunks, COUNT(DISTINCT COALESCE(json_extract(data,'$.document_id'),key)) AS documents, "
                 "COUNT(json_extract(data,'$.index_version')) AS indexed_chunks FROM records WHERE collection='memory'")
        parameters = []
        if run_id is not None:
            query += (" AND (json_extract(data,'$.run_id') IS NULL OR json_extract(data,'$.run_id') IN (?, 'global') "
                      "OR json_extract(data,'$.metadata.shared')=1)")
            parameters.append(run_id)
        with self._read_db() as db:
            row = db.execute(query, parameters).fetchone()
        return {**dict(row), 'method': 'sqlite-inverted-index+lexical+signed-feature-hash-512'}

    def batch(self, puts=(), deletes=(), *, supersede_validation=None):
        """Atomically commit a workflow transition and all its archived evidence."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            for collection, key in deletes:
                db.execute('DELETE FROM records WHERE collection=? AND key=?', (collection, key))
            for collection, key, value in puts:
                self._guard_write(db, collection, key, value)
                db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(collection,key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                           (collection, key, json.dumps(value, ensure_ascii=False, allow_nan=False), now()))
            if supersede_validation is not None:
                run_id, cycle, reason = supersede_validation
                self._guard_run(db, run_id)
                # Keep the byte-for-byte original receipt before changing only
                # lifecycle metadata. SQL copying avoids loading large arrays.
                db.execute("INSERT OR IGNORE INTO records(collection,key,data,updated_at) "
                    "SELECT 'experiment_original_receipts',key,data,updated_at FROM records "
                    "WHERE collection='experiments' AND json_extract(data,'$.run_id')=? "
                    "AND json_extract(data,'$.params.phase')='validation' "
                    "AND coalesce(json_extract(data,'$.research_cycle'),1)=? "
                    "AND coalesce(json_extract(data,'$.superseded'),0)=0",(run_id,cycle))
                # Legacy comparison payloads can exceed 600 MiB. Change only
                # lifecycle metadata in SQLite, never deserialize/copy arrays
                # through Python as part of activating an approved amendment.
                db.execute("UPDATE records SET data=json_set(data,'$.superseded',json('true'),"
                    "'$.superseded_reason',?),updated_at=? WHERE collection='experiments' "
                    "AND json_extract(data,'$.run_id')=? "
                    "AND json_extract(data,'$.params.phase')='validation' "
                    "AND coalesce(json_extract(data,'$.research_cycle'),1)=? "
                    "AND coalesce(json_extract(data,'$.superseded'),0)=0",
                    (reason, now(), run_id, cycle))

    def mutate(self, collection: str, key: str, fn: Callable, default=None):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
            self._guard_write(db, collection, key, json.loads(row['data']) if row else default,
                              before_mutation=True)
            value = fn(json.loads(row['data']) if row else default)
            self._guard_write(db, collection, key, value)
            db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(collection,key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                       (collection, key, json.dumps(value, ensure_ascii=False, allow_nan=False), now()))
            return value

    def event(self, run_id: str, kind: str, message: str, data: dict | None = None):
        timestamp = now()
        clean = redact(data or {})
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._guard_run(db, run_id)
            cursor = db.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
                                (run_id, kind, redact(message), json.dumps(clean, ensure_ascii=False), timestamp))
            return dict(id=cursor.lastrowid, run_id=run_id, kind=kind, message=redact(message), data=clean, created_at=timestamp)

    @staticmethod
    def _guard_run(db, run_id: str | None):
        if run_id and db.execute("SELECT 1 FROM records WHERE collection='run_deletions' AND key=?",
                                 (run_id,)).fetchone():
            raise RunDeletedError(f'Research {run_id} is being deleted or has been deleted')

    def _guard_write(self, db, collection: str, key: str, value: Any, *, before_mutation=False):
        if collection == 'usage':
            return  # Late settlement is a real charge, even after task deletion.
        if collection == 'runtime':
            if not before_mutation and isinstance(value, dict) and value.get('held'):
                self._guard_run(db, value.get('run_id'))
            return
        run_id = _run_owner(collection, key, value)
        if collection == 'budgets':
            if before_mutation or not isinstance(value, dict):
                return
            row = db.execute("SELECT data FROM records WHERE collection='budgets' AND key=?", (key,)).fetchone()
            previous = json.loads(row['data']) if row else {}
            if set(value.get('pending', {})) <= set(previous.get('pending', {})):
                return  # Existing reservations may be settled, never newly admitted.
        self._guard_run(db, run_id)
        if collection == 'runs' and not before_mutation and isinstance(value, dict):
            previous = db.execute("SELECT data FROM records WHERE collection='runs' AND key=?", (key,)).fetchone()
            old_status = json.loads(previous['data']).get('status') if previous else None
            status = value.get('status')
            if previous is not None and isinstance(status, str) and old_status != status:
                # Same transaction as every put/mutate/batch, including resume and rollback.
                # Never infer an active clock from non-atomic, later UI notifications.
                db.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
                    (key, 'run.status_changed', 'Research runtime state changed',
                     json.dumps({'previous_status': old_status, 'status': status}), now()))

    def begin_run_deletion(self, run_id: str, *, tombstone: dict, check_worker: Callable):
        """Install the deletion gate in the same transaction as the lease check."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute("SELECT data FROM records WHERE collection='run_deletions' AND key=?",
                                  (run_id,)).fetchone()
            if existing:
                return json.loads(existing['data'])
            if not db.execute("SELECT 1 FROM records WHERE collection='runs' AND key=?", (run_id,)).fetchone():
                raise ValueError('Research not found')
            worker = db.execute("SELECT data FROM records WHERE collection='runtime' AND key='worker'").fetchone()
            check_worker(json.loads(worker['data']) if worker else {})
            db.execute('INSERT INTO records VALUES (?,?,?,?)',
                       ('run_deletions', run_id, json.dumps(tombstone, ensure_ascii=False, allow_nan=False), now()))
            return tombstone

    def purge_run(self, run_id: str, *, tombstone: dict):
        """Atomically remove private task state; retain billing and shared knowledge."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM records WHERE collection='run_deletions' AND key=?", (run_id,)).fetchone():
                raise ValueError('A deletion marker is required before purging research')
            rows = db.execute('SELECT collection,key,data FROM records').fetchall()
            records = [(row['collection'], row['key'], json.loads(row['data'])) for row in rows]
            # Preserve only the safe usage projection of legacy native Codex
            # workflows before their task-owned prompts and artifacts are purged.
            from .codex_accounting import usage_record
            for collection, key, value in records:
                if collection != 'codex_workflows' or value.get('run_id') != run_id:
                    continue
                for attempt in value.get('attempts', []):
                    usage = usage_record(run_id, attempt.get('checkpoint'))
                    if usage:
                        db.execute('INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(collection,key) DO NOTHING',
                                   ('codex_usage', usage['id'], json.dumps(usage, ensure_ascii=False, allow_nan=False), now()))
            request_ids, transcript_ids = set(), set()
            for collection, key, value in records:
                if isinstance(value, dict) and _run_owner(collection, key, value) == run_id:
                    if collection in {'usage', 'transcripts'}:
                        request_ids.update(str(value[field]) for field in ('request_id',) if value.get(field))
                        if collection == 'usage':
                            request_ids.add(key)
                        else:
                            transcript_ids.add(key)
                    elif collection == 'budgets':
                        request_ids.update(str(item.get('id')) for item in value.get('records', []) if item.get('id'))
            for collection, key, value in records:
                if collection in SHARED_COLLECTIONS or collection in {'budgets', 'usage', 'codex_usage'}:
                    continue
                owned = _run_owner(collection, key, value) == run_id
                if collection == 'response_cache' and isinstance(value, dict) and not value.get('run_id'):
                    result = value.get('result') or {}
                    owned = owned or (isinstance(result, dict) and (
                        result.get('request_id') in request_ids or result.get('transcript_id') in transcript_ids))
                if not owned:
                    continue
                if collection == 'memory' and isinstance(value, dict) and value.get('metadata', {}).get('shared') is True:
                    value = {**value, 'run_id': None}
                    db.execute('UPDATE records SET data=?,updated_at=? WHERE collection=? AND key=?',
                               (json.dumps(value, ensure_ascii=False, allow_nan=False), now(), collection, key))
                else:
                    db.execute('DELETE FROM records WHERE collection=? AND key=?', (collection, key))
            db.execute('DELETE FROM events WHERE run_id=?', (run_id,))
            db.execute("UPDATE records SET data=?,updated_at=? WHERE collection='run_deletions' AND key=?",
                       (json.dumps(tombstone, ensure_ascii=False, allow_nan=False), now(), run_id))
            return tombstone

    def events(self, run_id: str, after: int = 0, limit: int = 500):
        with self._read_db() as db:
            rows = db.execute('SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?', (run_id, after, min(limit, 5000))).fetchall()
        return [{**dict(r), 'data': json.loads(r['data'])} for r in rows]

    def recent_events(self, run_id: str, limit: int = 500):
        with self._read_db() as db:
            rows = db.execute('SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?',
                              (run_id, max(1, min(limit, 5000)))).fetchall()
        return [{**dict(r), 'data': json.loads(r['data'])} for r in reversed(rows)]

    def role_tool_events(self, run_id: str, role: str, *, event_ids=None):
        """Read all matching headers, or full bodies for the selected page IDs.

        Role/kind filtering happens in SQLite before reading payloads. History
        pagination must not hide old tools behind the recent-events limit.
        """
        fields = 'id,run_id,kind,message,created_at' if event_ids is None else '*'
        query = (f'SELECT {fields} FROM events WHERE run_id=? '
                 "AND kind IN ('tool.started','tool.completed','tool.failed') AND json_extract(data,'$.role')=?")
        parameters = [run_id, role]
        if event_ids is not None:
            event_ids = list(event_ids)
            if not event_ids:
                return []
            query += ' AND id IN (' + ','.join('?' for _ in event_ids) + ')'
            parameters.extend(event_ids)
        with self._read_db() as db:
            rows = db.execute(query + ' ORDER BY id', parameters).fetchall()
        return [{**dict(row), **({'data': json.loads(row['data'])} if event_ids is not None else {})}
                for row in rows]

    def tool_call_detail(self, run_id: str, call_key: str):
        """Load one invocation's original events, scoped to its owning research.

        Keep legacy event IDs separate; never infer a missing start or result.
        Ambiguous correlation IDs cannot be presented as a single invocation.
        """
        if not isinstance(call_key, str) or len(call_key) > 256:
            return None
        if not (call_key.startswith('call:') and len(call_key) > 5 or
                re.fullmatch(r'event:[1-9][0-9]*', call_key)):
            return None
        page = self.tool_call_page(run_id, limit=1, call_key=call_key)
        return page['rows'][0] if page['total'] == 1 else None

    def tool_call_page(self, run_id: str, *, limit=30, offset=0, role=None, tool=None, status=None, call_key=None):
        """Page invocation headers before loading only this page's event payloads.

        Old events have no correlation ID and deliberately remain separate evidence
        records. A nearby start/completion is not sufficient to infer a pairing.
        """
        clauses = ["run_id=?", "kind IN ('tool.started','tool.completed','tool.failed')"]
        parameters = [run_id]
        if call_key is not None:
            if call_key.startswith('call:'):
                clauses.append("json_extract(data,'$.tool_call_id')=?")
                parameters.append(call_key[5:])
            else:
                clauses.extend(["json_extract(data,'$.tool_call_id') IS NULL", 'id=?'])
                parameters.append(call_key[6:])
        if role is not None:
            clauses.append("json_extract(data,'$.role')=?")
            parameters.append(role)
        if tool is not None:
            clauses.append('message=?')
            parameters.append(tool)
        cte = ("WITH selected AS (SELECT id,kind,message,created_at,"
               "json_extract(data,'$.role') AS role,"
               "CASE WHEN json_extract(data,'$.tool_call_id') IS NOT NULL "
               "THEN 'call:'||json_extract(data,'$.tool_call_id') ELSE 'event:'||id END AS call_key "
               "FROM events WHERE " + ' AND '.join(clauses) + "), grouped AS ("
               "SELECT call_key,message,role,MAX(id) AS last_id,"
               "MIN(CASE WHEN kind='tool.started' THEN id END) AS started_id,"
               "MAX(CASE WHEN kind IN ('tool.completed','tool.failed') THEN id END) AS finished_id,"
               "CASE WHEN MAX(kind='tool.failed')=1 THEN 'failed' "
               "WHEN MAX(kind='tool.completed')=1 THEN 'completed' ELSE 'started' END AS status "
               "FROM selected GROUP BY call_key,message,role) ")
        where = ' WHERE status=?' if status is not None else ''
        filtered = parameters + ([status] if status is not None else [])
        with self._read_db() as db:
            total = db.execute(cte + 'SELECT COUNT(*) FROM grouped' + where, filtered).fetchone()[0]
            headers = db.execute(cte + 'SELECT * FROM grouped' + where + ' ORDER BY last_id DESC LIMIT ? OFFSET ?',
                                 [*filtered, max(1, min(limit, 100)), max(0, offset)]).fetchall()
            event_ids = {row[field] for row in headers for field in ('started_id', 'finished_id') if row[field] is not None}
            bodies = {}
            if event_ids:
                rows = db.execute('SELECT * FROM events WHERE run_id=? AND id IN (' + ','.join('?' for _ in event_ids) + ')',
                                  [run_id, *event_ids]).fetchall()
                bodies = {row['id']: {**dict(row), 'data': json.loads(row['data'])} for row in rows}
        return {'total': total, 'rows': [{**dict(row), 'started': bodies.get(row['started_id']),
                                         'finished': bodies.get(row['finished_id'])} for row in headers]}


_MISSING = object()


class _ReadSnapshot(Store):
    """Read-only request-local memoization, never a cross-request evidence cache."""
    is_read_snapshot = True
    CACHE_BYTES = 8 * 1024 * 1024
    CACHE_ENTRY_BYTES = 2 * 1024 * 1024

    def __init__(self, source, db):
        self.path = source.path
        self._connection = db
        self._cache = OrderedDict()
        self._cache_sizes = {}
        self._cache_bytes = 0
        self._closed = False

    def _clear_cache(self):
        self._cache.clear()
        self._cache_sizes.clear()
        self._cache_bytes = 0

    @staticmethod
    def _size(value, limit):
        """Conservative object-graph estimate, stopping at the entry budget."""
        pending, seen, size = [value], set(), 0
        while pending:
            item = pending.pop()
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
            size += sys.getsizeof(item)
            if size > limit:
                return size
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple, set, frozenset)):
                pending.extend(item)
        return size

    def _cached_read(self, identity, builder, *, owned=False):
        self._require_open()
        if identity in self._cache:
            self._cache.move_to_end(identity)
            value = self._cache[identity]
        else:
            value = builder()
            limit = min(self.CACHE_ENTRY_BYTES, self.CACHE_BYTES)
            size = self._size((identity, value), limit) + 128
            if size > limit:
                # A fresh SQLite decode belongs only to this caller. Returning
                # it directly avoids a second whole-history tree; later reads
                # still use this same SQLite transaction and decode afresh.
                return value if owned else copy.deepcopy(value)
            while self._cache and self._cache_bytes + size > self.CACHE_BYTES:
                oldest, _ = self._cache.popitem(last=False)
                self._cache_bytes -= self._cache_sizes.pop(oldest)
            self._cache[identity] = value
            self._cache_sizes[identity] = size
            self._cache_bytes += size
        return value if value is _MISSING else copy.deepcopy(value)

    def _require_open(self):
        if self._closed:
            raise RuntimeError('Read snapshot has closed')

    @contextmanager
    def _db(self):
        self._require_open()
        yield self._connection

    _read_db = _db

    @contextmanager
    def read_snapshot(self):
        self._require_open()
        yield self

    def get(self, collection, key, default=None):
        identity = ('get', collection, key)
        value = self._cached_read(identity,
            lambda: super(_ReadSnapshot, self).get(collection, key, _MISSING), owned=True)
        return default if value is _MISSING else value

    def implementation_workflow_design(self, key):
        return self._cached_read(('implementation_workflow_design',key),
            lambda:super(_ReadSnapshot,self).implementation_workflow_design(key),owned=True)

    def _owned_record(self, collection, key, default=None):
        """Fresh private decode for one inventory authentication.

        Full manifests are consumed once to produce small public operands.
        Keeping them in the generic cache would copy their object graphs and
        evict those operands. This read uses the same transaction, retains no
        shared object, and cannot alter the normal detached get contract.
        """
        self._require_open()
        return super().get(collection, key, default)

    def list(self, collection, run_id=None):
        identity = ('list', collection, run_id)
        return self._cached_read(identity,
            lambda: super(_ReadSnapshot, self).list(collection, run_id), owned=True)

    def memoized_read(self, identity, builder):
        """Reuse one pure derived directory within this exact SQLite snapshot."""
        key = ('derived', identity)
        return self._cached_read(key, builder)

    def _readonly(self, *args, **kwargs):
        raise TypeError('Read snapshot cannot write')

    put = delete = mutate = batch = event = begin_deletion = purge_run = _readonly
