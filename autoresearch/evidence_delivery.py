"""Validate accessible evidence, separately from acquisition and tool execution.

Delivery proves that exact text was available in an acknowledged model request;
it cannot prove comprehension or the truth of a scientific claim. Legacy tool
ledgers remain readable, but cannot satisfy an explicit delivery requirement.
"""
from __future__ import annotations

import hashlib


class EvidenceDeliveryError(ValueError):
    """A recoverable evidence requirement failure, never a scientific verdict."""

    def __init__(self, message, *, code, details=None):
        super().__init__(message)
        self.details = {'code': code, 'recoverable': True, **(details or {})}


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _manifests(store, run_id, *, handoff_id=None, request_ids=None, role=None):
    wanted = set(request_ids) if request_ids is not None else None
    if hasattr(store, 'record_keys'):
        clauses, parameters = ['1'], []
        if handoff_id is not None:
            # Candidate lookup is SQL-equivalent to the old null fallback and
            # can use the metadata index. Original ownership is checked below:
            # explicit JSON null still cannot claim an operation's delivery.
            clauses.append("coalesce(json_extract(data,'$.handoff_id'),json_extract(data,'$.operation_id'))=?")
            parameters.append(handoff_id)
        if role is not None:
            clauses.append("json_extract(data,'$.role')=?")
            parameters.append(role)
        if wanted is not None:
            if not wanted:
                return []
            clauses.append("json_extract(data,'$.request_id') IN (" + ','.join('?' for _ in wanted) + ')')
            parameters.extend(sorted(wanted))
        keys = store.record_keys('context_manifests', run_id, predicate=' AND '.join(clauses),
                                 parameters=parameters)
        get = getattr(store, '_owned_record', store.get)
        candidates = [get('context_manifests', key) for key in keys]
    else:
        candidates = store.list('context_manifests', run_id)
    return [row for row in candidates if isinstance(row, dict)
            and row.get('run_id', row.get('research_id')) == run_id
            and (handoff_id is None or row.get('handoff_id', row.get('operation_id')) == handoff_id)
            and (role is None or row.get('role') == role)
            and (wanted is None or row.get('request_id') in wanted)]


def tracking_required(store, run_id, handoff_id):
    """Reuse only the read policy flag within one read-only validation.

    The exact snapshot or architecture operation owns this small boolean. An
    ordinary call and the next validation query again, including false results;
    no submitted inventory, delivery verdict or source-file proof is cached.
    Legacy paid/frozen handoffs keep the existing metadata lookup semantics.
    """
    if not handoff_id:
        return False

    def lookup():
        if hasattr(store, 'record_keys'):
            return bool(store.record_keys('context_manifests', run_id,
                predicate="coalesce(json_extract(data,'$.handoff_id'),json_extract(data,'$.operation_id'))=?",
                parameters=[handoff_id], limit=1))
        return bool(_manifests(store, run_id, handoff_id=handoff_id))

    identity = ('evidence_delivery_tracking_required', run_id, handoff_id)
    if getattr(store, 'is_read_snapshot', False):
        return store.memoized_read(identity, lookup)
    from .architecture_validation import source_read
    return source_read(store, identity, lookup)


def _submitted_manifests(store, run_id, role, handoff_id, request_ids):
    """Authenticate a scope once per SQLite snapshot, retaining only operands.

    Each document in a reading card uses the same submitted inventory. The
    compact result avoids repeatedly copying full compiled context histories;
    it expires with the read transaction and never caches a file or approval.
    Ordinary execution callers still authenticate every inventory afresh.
    """
    wanted = tuple(sorted(set(request_ids))) if request_ids is not None else None

    def authenticate():
        manifests = {row.get('request_id'): row for row in
                     _manifests(store, run_id, handoff_id=handoff_id, request_ids=wanted, role=role)
                     if row.get('submission_status', row.get('status')) in {'submitted', 'completed'}
                     and row.get('role') == role and row.get('request_id')}
        from .context_manifest import validate_manifest
        result = {}
        material_fields = ('kind', 'compiled', 'submitted', 'text_deliverable',
            'source_id', 'document_id', 'sha256', 'text_sha256', 'char_start',
            'char_end', 'delivered_text_sha256')
        for key, row in manifests.items():
            if not validate_manifest(store, row):
                continue
            compact = {name: row[name] for name in ('id', 'snapshot_id') if name in row}
            if 'materials' in row:
                compact['materials'] = [{name: item[name] for name in material_fields if name in item}
                    for item in row['materials'] if item.get('kind') == 'evidence']
            result[key] = compact
        return result

    if getattr(store, 'is_read_snapshot', False):
        return store.memoized_read(('submitted_delivery_manifests', run_id, role, handoff_id, wanted), authenticate)
    return authenticate()


def delivery_spans(store, run_id, role, identifier, sha256, *, identity_key='source_id',
                   handoff_id=None, request_ids=None, version=None, text=None, aliases=()):
    """Return hash-checked exact ranges from submitted requests of this role.

    Candidate/compiled manifests, search excerpts, unknown network attempts and
    another role's independent thread never confer reading credit.
    """
    if identity_key not in {'source_id', 'document_id'}:
        raise ValueError('Evidence identity must be a source or document ID')
    manifests = _submitted_manifests(store, run_id, role, handoff_id, request_ids)
    spans = []
    if hasattr(store, 'record_keys'):
        identifiers = sorted({identifier, *aliases})
        predicate = "json_extract(data,'$.role')=? AND json_extract(data,'$." + identity_key + "') IN (" + ','.join('?' for _ in identifiers) + ')'
        parameters = [role, *identifiers]
        if handoff_id is not None:
            # Apply the existing receipt ownership check before decoding this
            # run's old reads of the same document. Missing legacy handoff_id
            # may use operation_id; explicit JSON null still fails .get below.
            from .storage import _DELIVERY_HANDOFF
            predicate += ' AND (' + _DELIVERY_HANDOFF + ')=?'
            parameters.append(handoff_id)
        keys = store.record_keys('evidence_deliveries', run_id, predicate=predicate,
            parameters=parameters)
        get = getattr(store, '_owned_record', store.get)
        candidates = [get('evidence_deliveries', key) for key in keys]
    else:
        candidates = store.list('evidence_deliveries', run_id)
    for receipt in candidates:
        if not isinstance(receipt, dict):
            continue
        request_id = receipt.get('request_id')
        manifest = manifests.get(request_id)
        if (not manifest or receipt.get('run_id', receipt.get('research_id')) != run_id
                or receipt.get('role') != role or receipt.get('status') != 'delivered'
                or receipt.get(identity_key) not in {identifier, *aliases}
                or receipt.get('sha256', receipt.get('text_sha256')) != sha256
                or handoff_id is not None and receipt.get('handoff_id', receipt.get('operation_id')) != handoff_id
                or version is not None and next((receipt[name] for name in
                    ('document_version', 'source_version', 'version', 'rag_version_id', 'version_id')
                    if receipt.get(name) is not None), receipt.get('sha256')) != version):
            continue
        if (receipt.get('manifest_id', manifest.get('id')) != manifest.get('id')
                or receipt.get('snapshot_id') is not None and manifest.get('snapshot_id') is not None
                and receipt['snapshot_id'] != manifest['snapshot_id']):
            continue
        start = receipt.get('char_start', receipt.get('offset'))
        end = receipt.get('char_end', receipt.get('end'))
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            continue
        if text is not None:
            if end > len(text) or receipt.get('page_sha256') != _sha(text[start:end]):
                continue
        if 'materials' in manifest:
            # Receipts alone cannot turn a candidate/search result into a
            # submitted passage; authenticate against the request inventory.
            found = False
            for material in manifest['materials']:
                material_sha = (material.get('sha256') if material.get('document_id')
                    else material.get('text_sha256')) or material.get('sha256')
                if (material.get('kind') == 'evidence' and material.get('compiled') is True
                        and material.get('submitted') is True and material.get('text_deliverable') is True
                        and material.get(identity_key) in {identifier, *aliases}
                        and material_sha == sha256 and material.get('char_start') == start
                        and material.get('char_end') == end
                        and material.get('delivered_text_sha256') == receipt.get('page_sha256')):
                    found = True
                    break
            if not found:
                continue
        spans.append({**receipt, 'char_start': start, 'char_end': end})
    return sorted(spans, key=lambda row: (row['char_start'], row['char_end'],
                                         row['request_id'], row.get('id', '')))


def covering_receipts(receipts, start, end, *, section_id=None):
    """Choose deterministic receipts covering the requested Unicode range."""
    cursor, used = start, []
    for row in sorted(receipts, key=lambda value: (value['char_start'], -value['char_end'],
                                                 value.get('created_at', ''), value.get('request_id', ''),
                                                 value.get('id', ''))):
        if section_id is not None and row.get('section_id') != section_id:
            continue
        if row['char_start'] <= cursor < row['char_end']:
            used.append(row)
            cursor = row['char_end']
            if cursor >= end:
                return used
    return []


def validate_evidence_requirements(engine, run_id, role, requirements, *, request_ids=None,
                                   handoff_id=None):
    """Fail closed for explicit verified conclusions or version-bound reviews.

    Ordinary generation does not call this gate. Each requirement binds a
    source/document, full-text SHA256 and required [char_start,char_end) range;
    optional version/current_version bindings reject stale scientific sources.
    Source loading reuses the existing immutable-file and ownership checks.
    """
    if not isinstance(requirements, list) or not requirements:
        raise EvidenceDeliveryError('Verified conclusions require explicit evidence ranges',
                                    code='evidence_required')
    reports = []
    for item in requirements:
        if not isinstance(item, dict):
            raise EvidenceDeliveryError('Evidence requirement must be an object', code='invalid_requirement')
        key = 'document_id' if item.get('document_id') else 'source_id'
        identifier, sha = item.get(key), item.get('sha256', item.get('text_sha256'))
        if (not isinstance(identifier, str) or not identifier or not isinstance(sha, str)
                or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha)):
            raise EvidenceDeliveryError('Evidence requires an exact ID and lowercase SHA256',
                                        code='invalid_identity')
        try:
            if key == 'document_id':
                from .document_index import load
                source, text = load(engine, run_id, identifier, sha)
            else:
                source = engine.sources.document(run_id, identifier)
                text = source['text']
            actual_sha = source.get('text_sha256', source.get('sha256'))
            version = next((source[name] for name in
                ('document_version', 'source_version', 'version', 'rag_version_id', 'version_id')
                if source.get(name) is not None), actual_sha)
            paper_id = source.get('paper_id') or item.get('paper_id')
            current = (engine.store.get('run_evidence', f'{run_id}:{paper_id}', {}).get('reading') or {}) if paper_id else {}
            # Historical immutable snapshots remain recoverable and may support
            # unverified notes. Verified current conclusions use the active
            # successfully acquired paper source, rather than reading_history.
            current_sha = current.get('text_sha256') if current.get('retrieval_status') in {
                'pdf_text_indexed', 'web_text_indexed'} else None
            if (actual_sha != sha or _sha(text) != sha
                    or source.get('superseded_by') or source.get('invalidated_at')
                    or source.get('status') in {'invalid', 'invalidated', 'deleted', 'superseded', 'unavailable'}
                    or source.get('source_status') in {'invalid', 'invalidated', 'deleted', 'superseded', 'unavailable'}
                    or item.get('version') is not None and version != item['version']
                    or current_sha is not None and current_sha != sha
                    or item.get('current_version') is not None and version != item['current_version']
                    or item.get('current_sha256') is not None and sha != item['current_sha256']):
                raise ValueError('Evidence source is obsolete or its version/SHA256 does not match')
        except (ValueError, OSError, KeyError) as exc:
            raise EvidenceDeliveryError(str(exc), code='source_version_mismatch',
                details={key: identifier, 'sha256': sha, 'next_step': 'Resolve the active source version and read its exact range.'}) from exc
        start, end = item.get('char_start'), item.get('char_end')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise EvidenceDeliveryError('Evidence range is outside the retained source', code='invalid_range',
                                        details={key: identifier})
        receipts = delivery_spans(engine.store, run_id, role, identifier, sha, identity_key=key,
            handoff_id=handoff_id, request_ids=request_ids, version=item.get('version'), text=text,
            aliases=[source[name] for name in ('source_alias_id', 'canonical_source_id')
                     if key == 'source_id' and source.get(name)])
        used = covering_receipts(receipts, start, end, section_id=item.get('section_id'))
        if not used:
            raise EvidenceDeliveryError('Required evidence was not delivered to an acknowledged model request',
                code='evidence_not_delivered', details={key: identifier, 'sha256': sha,
                'char_start': start, 'char_end': end, 'next_step': 'Read the same version and submit a request containing the exact required range.'})
        reports.append({key: identifier, 'sha256': sha, 'char_start': start, 'char_end': end,
            'request_ids': sorted({row['request_id'] for row in used}),
            'delivery_receipt_ids': [row['id'] for row in used], 'status': 'delivered'})
    return {'requirements': reports, 'delivery_verified': True, 'scientific_claims_verified': False}
