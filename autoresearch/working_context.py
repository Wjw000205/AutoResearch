"""Disposable working views backed by immutable receipts and committed outputs.

Compilation is read-only. A successful model response commits the full receipt
archive and state in one transaction before any active view can evict it.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

from .storage import now, redact


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def receipt_id(run_id, row):
    return f'{run_id}:context-receipt:{sha(row)}'


def document_id(run_id, row):
    call, page = row.get('call', {}), row.get('result', {})
    return f'{run_id}:context-document:' + sha([call.get('name'),
        page.get('artifact_id', page.get('rag_version_id')), page.get('sha256'), page.get('section')])


def validate_commit(store, run_id, role, marker):
    if not marker or not marker.get('commit_id'):
        return None
    saved = store.get('context_commits', marker['commit_id'])
    if (not saved or saved.get('run_id') != run_id or saved.get('role') != role
            or saved.get('handoff_id') != marker.get('handoff_id')
            or saved.get('sha256') != sha(saved.get('payload'))):
        raise ValueError('Working context commit identity or integrity mismatch')
    from .scientific_state import project
    if project(saved['payload']['state_source']) != saved['payload']['state']:
        raise ValueError('Working state cannot be rebuilt from committed outputs')
    for rid in saved['payload']['receipt_ids']:
        receipt = store.get('context_receipts', rid)
        if (not receipt or receipt.get('run_id') != run_id
                or rid != receipt_id(run_id, receipt.get('receipt'))
                or receipt.get('sha256') != sha(receipt.get('receipt'))):
            raise ValueError('Committed context receipt identity or integrity mismatch')
    return saved


def marker(run_id, role, key, progress):
    return {'version': 3, 'handoff_id': key, 'role': role,
            'commit_id': progress.get('working_context_commit')}


def validate_first_turn(store, run_id, role, key, prepared, result, progress, *, response_id):
    """Authenticate a first-turn closeout without granting save/read credit.

    This narrow compatibility check does not admit a previous commit, output
    recovery or additional state. Later turns continue through their own gates.
    Return all external records so the caller can guard them at publication.
    """
    from .prose_schema import relax, POLICY
    from jsonschema import Draft202012Validator
    saved = validate_commit(store, run_id, role, marker(run_id, role, key, progress))
    if not saved:
        raise ValueError('Missing first-turn working context commit')
    payload = saved['payload']
    context = prepared['context']
    if ('_working_set' in context
            and context['_working_set'] != marker(run_id, role, key, {})):
        raise ValueError('First-turn working context marker version or identity mismatch')
    schema = prepared['schema']
    effective = relax(schema)
    Draft202012Validator(effective).validate(result)
    validation = ({'output_validation': {'prose_policy': POLICY,
        'original_schema_sha256': sha(schema), 'effective_schema_sha256': sha(effective)}}
        if effective != schema else {})
    source = payload['state_source']
    # Goal and stage are the original host snapshot, not today's mutable run
    # stage. The output-derived state and request-owned values must be exact.
    expected_source = {'research_goal': source['research_goal'], 'stage': source['stage'],
        'research_cycle': context.get('research_cycle', 0),
        'outputs': {'turn_' + sha(result): result},
        'execution_constraints': context.get('execution_constraints', {}),
        'provenance': 'schema_validated_model_turn_not_scientific_verification'}
    rows = context.get('tool_results', [])
    expected = {'run_id': run_id, 'role': role, 'handoff_id': key,
        'prepared_sha256': sha(prepared), 'response_id': response_id,
        **validation, 'output': result, 'state_source': expected_source,
        'state': payload['state'], 'receipt_ids': [receipt_id(run_id, row) for row in rows],
        'previous_commit': None}
    if (progress.get('turn') != 0 or prepared.get('role') != role
            or rows != progress.get('tool_results', []) or payload != expected
            or set(saved) != {'id', 'run_id', 'role', 'handoff_id', 'sha256', 'payload', 'created_at'}
            or saved.get('id') != progress.get('working_context_commit')
            or saved['id'] != f'{run_id}:context-commit:{sha(payload)}'):
        raise ValueError('Working context closeout differs from its original request/output/receipts')
    return [('context_commits', saved['id'], saved), *[
        ('context_receipts', rid, store.get('context_receipts', rid)) for rid in payload['receipt_ids']]]


def commit(store, run_id, role, key, prepared, result, progress, *, response_id=None):
    """Called only after schema validation, never on an unknown/invalid response."""
    from jsonschema import Draft202012Validator
    from .scientific_state import project
    from .rapid_output_length import validation_audit, schema_without_code_limits
    length_audit = validation_audit(store, run_id, key, prepared, result, progress)
    schema = schema_without_code_limits(prepared['schema']) if length_audit else prepared['schema']
    from .prose_schema import relax as prose_schema, POLICY as prose_policy
    effective = prose_schema(schema)
    if effective != schema:
        length_audit = {**(length_audit or {}), 'prose_policy': prose_policy,
                       'original_schema_sha256': sha(schema), 'effective_schema_sha256': sha(effective)}
    schema = effective
    Draft202012Validator(schema).validate(result)
    rows = prepared['context'].get('tool_results', [])
    records = []
    documents = {}
    for row in rows:
        if redact(row) != row:
            raise ValueError('Tool receipt must be redacted before a working commit')
        rid = receipt_id(run_id, row)
        old = store.get('context_receipts', rid)
        if old and (old.get('sha256') != sha(row) or old.get('receipt') != row):
            raise ValueError('Immutable context receipt changed')
        records.append(('context_receipts', rid, old or {
            'id': rid, 'run_id': run_id, 'sha256': sha(row),
            'receipt': copy.deepcopy(redact(row)), 'created_at': now()}))
        # Freeze the complete version now, before subsequent tools can rewrite a
        # catalog or change dynamic RAG presentation. Page receipts alone cannot
        # reconstruct a complete document when some pages were never requested.
        try:
            parent = _parent(store, run_id, row)
            if parent:
                _, text = parent
                did = document_id(run_id, row)
                existing = store.get('context_documents', did)
                record = existing or {'id': did, 'run_id': run_id,
                    'sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'text': text, 'created_at': now(), 'kind': row['call']['name']}
                documents[did] = ('context_documents', did, record)
        except (ValueError, KeyError, TypeError, OSError):
            pass  # Unverifiable parents stay raw; their receipts still archive.
    previous = validate_commit(store, run_id, role, marker(run_id, role, key, progress))
    if (previous and previous['payload']['prepared_sha256'] == sha(prepared)
            and previous['payload']['output'] == result):
        return previous['id']
    outputs = copy.deepcopy(previous['payload']['state_source']['outputs']) if previous else {}
    outputs['turn_' + sha(result)] = copy.deepcopy(result)
    source = {'research_goal': store.get('runs', run_id, {}).get('objective', ''),
        'stage': store.get('runs', run_id, {}).get('stage'),
        'research_cycle': prepared['context'].get('research_cycle', 0),
        'outputs': outputs,
        'execution_constraints': copy.deepcopy(prepared['context'].get('execution_constraints', {})),
        'provenance': 'schema_validated_model_turn_not_scientific_verification'}
    payload = {'run_id': run_id, 'role': role, 'handoff_id': key,
        'prepared_sha256': sha(prepared), 'response_id': response_id,
        **({'output_validation': length_audit} if length_audit else {}),
        'output': copy.deepcopy(result), 'state_source': source, 'state': project(source),
        'receipt_ids': [record[1] for record in records],
        'previous_commit': progress.get('working_context_commit')}
    digest = sha(payload)
    cid = f'{run_id}:context-commit:{digest}'
    previous = store.get('context_commits', cid)
    record = previous or {'id': cid, 'run_id': run_id, 'role': role,
        'handoff_id': key, 'sha256': digest, 'payload': payload, 'created_at': now()}
    if previous and previous['payload'] != payload:
        raise ValueError('Immutable working state changed')
    updated = {**copy.deepcopy(progress), 'id': key, 'run_id': run_id, 'working_context_commit': cid}
    store.batch(puts=[*documents.values(), *records,
        ('context_commits', cid, record), ('handoff_progress', key, updated)])
    progress.update(updated)
    return cid


def _page_matches(row, text):
    page, args = row['result'], row.get('call', {}).get('arguments', {})
    offset = page.get('offset')
    return (type(offset) is int and offset >= 0 and args.get('offset', 0) == offset
            and page.get('total_chars') == len(text)
            and hashlib.sha256(text.encode()).hexdigest() == page.get('sha256')
            and text[offset:offset+len(page['text'])] == page['text'])


def _parent(store, run_id, row):
    """Resolve only a current, hash-matching structured parent of a read page."""
    from .context_artifacts import _object
    from .deletion import _plain_path
    call, page = row.get('call', {}), row.get('result', {})
    if not isinstance(page, dict) or not isinstance(page.get('text'), str):
        return None
    name, args = call.get('name'), call.get('arguments', {})
    archived = store.get('context_documents', document_id(run_id, row))
    if archived and name in {'artifacts.read', 'literature.rag_read'}:
        if (archived.get('run_id') != run_id or archived.get('kind') != name
                or archived.get('sha256') != page.get('sha256')
                or not isinstance(archived.get('text'), str)
                or hashlib.sha256(archived['text'].encode()).hexdigest() != page.get('sha256')):
            raise ValueError('Immutable context document identity or integrity mismatch')
        if (name == 'artifacts.read' and args.get('id') != page.get('artifact_id')
                or name == 'literature.rag_read' and (page.get('section') != 'analysis'
                    or args.get('version_id') != page.get('rag_version_id'))):
            return None
        text = archived['text']
    elif name == 'artifacts.read':
        record = store.get('artifacts', page.get('artifact_id'))
        if (not record or record.get('run_id') != run_id
                or args.get('id') != record['id'] or record.get('sha256') != page.get('sha256')):
            return None
        path = Path(record['path']).resolve()
        root = (store.path.parent / 'runs' / run_id).resolve()
        if not path.is_relative_to(root) or path.stat().st_size > 4 * 1024 * 1024:
            return None
        _plain_path(path, store.path.parent)
        # artifacts.read hashes Unicode text after universal newline conversion,
        # not the CRLF bytes Windows may have written to the file.
        text = path.read_text(encoding='utf-8')
        if hashlib.sha256(text.encode()).hexdigest() != page['sha256']:
            return None
    elif name == 'literature.rag_read' and page.get('section') == 'analysis':
        from .paper_rag import PaperRAG
        from .paper_rag_curation import analysis_bundle
        if args.get('version_id') != page.get('rag_version_id'):
            return None
        rag = PaperRAG(store, store.path.parent)
        payload = rag.load(page['rag_version_id'])
        bundle = analysis_bundle(rag, payload)
        # The display layer can enrich paper metadata after this version was
        # read. Try the version's original paper as a known older rendition;
        # never strip arbitrary fields or accept only a matching page prefix.
        renditions = [bundle]
        if isinstance(payload.get('paper'), dict):
            historical = copy.deepcopy(bundle)
            historical['paper'] = copy.deepcopy(payload['paper'])
            renditions.append(historical)
        text = None
        for rendition in renditions:
            # Older pages also predate these two presentation fields. The
            # entire serialized parent must match SHA256, length and page text.
            legacy = {key: value for key, value in rendition.items()
                      if key not in {'provenance', 'analysis_created_at'}}
            for candidate in (rendition, legacy):
                candidate_text = json.dumps(candidate, ensure_ascii=False, indent=2)
                if hashlib.sha256(candidate_text.encode()).hexdigest() == page.get('sha256'):
                    text = candidate_text
                    break
            if text is not None:
                break
        if text is None:
            return None
    else:
        return None
    if len(text.encode()) > 4 * 1024 * 1024:
        return None
    if not _page_matches(row, text):
        return None
    document = json.loads(text, object_pairs_hook=_object)
    if not isinstance(document, dict):
        return None
    if name == 'artifacts.read' and not isinstance(document.get('papers'), list):
        return None  # Arbitrary code, proofs and other artifacts stay exact.
    return document, text


def structured_view(document):
    """Project known bibliographic/prose details, preserving unknown extensions.

Measurements, uncertainty, rejection reasons and source anchors stay verbatim.
This view describes historical records, never a new reading certificate.
"""
    from .context_compiler import paper_card
    from .compression_safety import MEASUREMENT, DECISIVE_CAVEAT
    def walk(value, field=''):
        if isinstance(value, list):
            return [walk(item, field) for item in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        if field in {'paper', 'papers'} and value.get('id'):
            return paper_card(value)
        result = {}
        for key, item in value.items():
            if key in {'excerpts', 'citations'} and isinstance(item, list):
                # Retain exact quotes used by measurements; long background
                # excerpts remain under the version-bound archive/expand tool.
                result[key] = [walk({k: copy.deepcopy(v) for k,v in entry.items() if (
                    k != 'quote' or MEASUREMENT.search(str(v)) or DECISIVE_CAVEAT.search(str(v))
                    or entry.get('compression_class') in {'A', 'B'} or entry.get('kind') in {
                        'counterevidence', 'negative_result', 'limitation', 'failed_to_replicate',
                        'uncertainty', 'alternative_explanation', 'anomaly'})}, key)
                    if isinstance(entry, dict) else copy.deepcopy(entry) for entry in item]
            elif key in {'created_at', 'source_read_receipts', 'source_read_event_ids',
                         'memory_chunk_ids', 'memory_document_id', 'source_transcription'}:
                continue
            else:
                result[key] = walk(item, key)
        return result
    view = walk(document)
    return {'record': view, 'record_sha256': sha(document),
        'provenance': 'exact_projection_of_hash_verified_historical_records',
        'scientific_claims_verified': False, 'primary_source_reading': False,
        'coverage': 'Only selected stored records; undisplayed excerpt text is retrievable, not a new reading.'}


def completed_note_indexes(store, run_id, rows, consumed):
    """Historical save indexes only; these do not revalidate or approve notes."""
    if store.get('runs', run_id, {}).get('config', {}).get('completed_literature_context_policy') != 'index':
        return {}
    from .literature_notes import NOTE_FIELDS, _digest
    indexes = {}
    for row in rows:
        if (receipt_id(run_id, row) not in consumed
                or row.get('call', {}).get('name') != 'literature.record_notes'):
            continue
        result = row.get('result', {})
        if not isinstance(result, dict) or result.get('error') or not isinstance(result.get('notes'), list):
            continue
        for candidate in result['notes']:
            if not isinstance(candidate, dict) or not candidate.get('id'):
                continue
            note = store.get('literature_notes', candidate['id'])
            if (not isinstance(note, dict) or not all(key in note for key in NOTE_FIELDS)
                    or note.get('run_id') != run_id or note.get('generation') != 'ai_extraction'
                    or note.get('note_sha256') != _digest([run_id, {k: note[k] for k in NOTE_FIELDS}])
                    or note.get('id') != f"{run_id}:literature-note:{note.get('note_sha256')}"
                    or any(note.get(key) != value for key,value in candidate.items())):
                continue
            indexes[note['id']] = {'note_id': note['id'], **{key: copy.deepcopy(note[key]) for key in (
                'paper_id', 'source_id', 'text_sha256', 'note_sha256', 'reading_scope', 'generation')},
                'saved': True, 'new_reading_credit': False, 'scientific_claims_verified': False}
    return indexes


def saved_analysis_index(document, page):
    """Index a hash-verified, consumed RAG analysis in the existing L4 view.

    The full L2 record and L0 receipt remain in context_documents/receipts and
    PaperRAG. An index is historical navigation, never displayed support.
    """
    analysis, paper = document.get('analysis'), document.get('paper')
    if (not isinstance(analysis, dict) or not isinstance(paper, dict)
            or not all(isinstance(analysis.get(key), str) and analysis[key]
                       for key in ('id', 'text_sha256', 'generation'))
            or not page.get('rag_version_id')):
        return None
    return {'record': {
        'paper': {key: copy.deepcopy(paper[key]) for key in ('id', 'title', 'year', 'verified') if key in paper},
        'rag_version_id': page['rag_version_id'],
        'analysis_index': {key: copy.deepcopy(analysis[key]) for key in (
            'id', 'note_sha256', 'source_id', 'text_sha256', 'reading_scope', 'generation',
            'paper_identity_verified', 'document_identity_verified', 'source_text_truncated') if key in analysis},
        'origin_run_id': document.get('origin_run_id'),
        'provenance': document.get('provenance'),
        'analysis_created_at': document.get('analysis_created_at'),
        'new_reading_credit': False, 'scientific_claims_verified': False},
        'record_sha256': sha(document), 'completed_reading_directory': True,
        'primary_source_reading': False, 'scientific_claims_verified': False,
        'archived_fields': [{'path': '/' + key.replace('~', '~0').replace('/', '~1'),
                            'sha256': sha(value), 'chars': len(encoded(value))}
                           for key,value in document.items()],
        'coverage': 'Index of an already saved and consumed RAG analysis, per user policy. '
            'No analysis prose, measurements or contrary findings are displayed by this index. '
            'Use the version-bound context.expand paths (for example /analysis/limitations) '
            'before a judgment requiring those details. Historical reading scope and provenance '
            'remain authoritative; this does not grant new reading or scientific approval.'}


def prepare(store, run_id, role, context, *, enabled=False, task=''):
    """Read-only and deterministic; cache parents only within this compilation."""
    mark = context.get('_working_set', {})
    if not enabled and mark.get('version') != 3:
        return None
    if mark and mark.get('role') != role:
        raise ValueError('Working context belongs to another role')
    saved = validate_commit(store, run_id, role, mark)
    consumed = set(saved['payload']['receipt_ids']) if saved else set()
    if saved:
        # An explicit reread can be byte-identical to its old receipt. Hash-set
        # membership alone incorrectly compressed that new delivery again.
        previous_counts = Counter(saved['payload']['receipt_ids'])
        current_counts = Counter(receipt_id(run_id, row) for row in context.get('tool_results', [])
                                 if isinstance(row, dict))
        consumed -= {rid for rid,count in current_counts.items() if count > previous_counts[rid]}
    indexes = completed_note_indexes(store, run_id, context.get('tool_results', []), consumed)
    views, parents = {}, {}
    for row in context.get('tool_results', []):
        if not isinstance(row, dict):
            continue
        rid = receipt_id(run_id, row)
        resolved = row
        if row.get('call', {}).get('name') == 'context.expand':
            args = row['call'].get('arguments', {})
            archived = store.get('context_receipts', args.get('receipt_id'))
            output = row.get('result', {})
            if (isinstance(output, dict) and archived and archived.get('run_id') == run_id
                    and archived.get('sha256') == sha(archived.get('receipt')) == args.get('expected_sha256')
                    and output.get('receipt_id') == archived['id']
                    and output.get('receipt_sha256') == archived['sha256']
                    and output.get('original_tool') == archived['receipt'].get('call', {}).get('name')
                    and output.get('result') == archived['receipt'].get('result')):
                resolved = archived['receipt']
        page = resolved.get('result', {})
        if not isinstance(page, dict) or page.get('error'):
            continue
        call = resolved.get('call', {})
        if (rid in consumed and call.get('name') in {
                'pdf.analyze_figure', 'literature.record_notes',
                'literature.record_protocol', 'literature.protocols'}):
            # These results are already immutable, role-bound receipts. They
            # are historical structured records just like consumed RAG pages;
            # retaining every complete result bypassed the working-set budget.
            # Fresh tool results and errors remain exact. guard_view keeps
            # contrary facts and indexes undisplayed fields for exact expansion.
            views[rid] = {**structured_view(page), 'archived_fields': [
                {'path': '/' + key.replace('~', '~0').replace('/', '~1'),
                 'sha256': sha(value), 'chars': len(encoded(value))} for key,value in page.items()]}
            continue
        identity = (call.get('name'), page.get('artifact_id', page.get('rag_version_id')),
                    page.get('sha256'), page.get('section'))
        try:
            if identity not in parents:
                parents[identity] = _parent(store, run_id, resolved)
            parent = parents[identity]
            if parent is not None:
                # Every page is checked against the parent, not merely the first.
                doc, text = parent
                if not _page_matches(resolved, text):
                    continue
                if (call.get('name') == 'artifacts.read' and call.get('arguments', {}).get('id') != page.get('artifact_id')
                        or call.get('name') == 'literature.rag_read' and call.get('arguments', {}).get('version_id') != page.get('rag_version_id')):
                    continue
                if (rid in consumed and call.get('name') == 'literature.rag_read'
                        and store.get('runs', run_id, {}).get('config', {}).get(
                            'completed_literature_context_policy') == 'index'):
                    directory = saved_analysis_index(doc, page)
                    if directory is not None:
                        views[rid] = directory
                        continue
                views[rid] = structured_view(doc)
        except (ValueError, KeyError, TypeError, OSError):
            continue  # Unknown/tampered parents keep their exact original pages.
    return {'views': views, 'consumed': consumed, 'commit': saved,
            'completed_notes': indexes,
            'marker': mark, 'enabled': True, 'query': '\n'.join(str(v) for v in (
                store.get('runs', run_id, {}).get('objective', ''), context.get('selected_idea', ''),
                context.get('proposal', ''), context.get('unresolved_issues', ''), task.split('FINAL OUTPUT CONTRACT')[0]))}


def guard_view(view):
    """Exact counterevidence inventory for unselected historical records.

    Other archived facts are deliberately not represented as displayed support.
    All source material and schema extensions remain in the immutable archive.
    """
    if view.get('completed_reading_directory') is True:
        return copy.deepcopy(view)  # Already an index; retain every version/locator.
    from .compression_safety import ADVERSE_FIELDS, ADVERSE_KINDS, DECISIVE_CAVEAT
    fields = ADVERSE_FIELDS | {'remaining_gaps', 'issues', 'warnings', 'errors', 'retrieval_errors',
        'scope_notice', 'scope_note', 'scientific_claims_verified',
        'document_identity_verified', 'paper_identity_verified', 'source_text_truncated',
        'source_untrusted', 'grants_task_reading_credit', 'grants_novelty_approval'}
    inventory = []
    def walk(value, path=''):
        if isinstance(value, list):
            for i,item in enumerate(value):
                walk(item, path + '/' + str(i))
        elif isinstance(value, dict):
            if (value.get('compression_class') in {'A', 'B'}
                    or value.get('kind') in ADVERSE_KINDS
                    or value.get('approve') is False or value.get('approved') is False):
                inventory.append({'source_path': path, 'value': copy.deepcopy(value)})
                return
            for key,item in value.items():
                pointer = path + '/' + key.replace('~', '~0').replace('/', '~1')
                if key in fields and item not in (None, '', [], {}):
                    inventory.append({'source_path': pointer, 'value': copy.deepcopy(item)})
                elif isinstance(item, str) and key in {'text', 'summary', 'results'} and DECISIVE_CAVEAT.search(item):
                    inventory.append({'source_path': pointer, 'value': copy.deepcopy(item)})
                else:
                    walk(item, pointer)
    document = view['record']
    walk(document)
    identity = {key: copy.deepcopy(document[key]) for key in ('paper', 'id', 'paper_id', 'source_id', 'text_sha256') if key in document}
    if isinstance(document.get('papers'), list):
        identity['papers'] = [{key: copy.deepcopy(paper[key]) for key in ('id', 'title', 'verified') if key in paper}
                              for paper in document['papers'] if isinstance(paper, dict)]
    return {**copy.deepcopy({key: value for key,value in view.items() if key != 'record'}),
        'record': {**identity, 'counterevidence_inventory': inventory},
        'selection': {'detail_level': 'archived_record_with_exact_counterevidence_inventory',
            'historical_results_displayed': False,
            'policy': 'Unselected historical results, methods, prose, unknown extensions and their anchors remain in the version-bound archive. This directory is not support or a complete reading; expand before a judgment needing those fields.'}}


def retrieval_directory(passage, estimate):
    """Compact only a known, committed retrieval range; unknown shapes stay exact."""
    from .compression_safety import decisive_spans
    if not isinstance(passage, dict):
        return copy.deepcopy(passage)
    known = {'source_id', 'text_sha256', 'section', 'start', 'end', 'quote', 'locator',
             'adverse_candidate', 'score', 'semantic_score', 'hybrid_score'}
    quote, start, end = passage.get('quote'), passage.get('start'), passage.get('end')
    source, digest = passage.get('source_id'), passage.get('text_sha256')
    if (set(passage) - known or not isinstance(quote, str) or type(start) is not int or type(end) is not int
            or start < 0 or end - start != len(quote) or not isinstance(source, str) or not source
            or not isinstance(digest, str) or len(digest) != 64
            or any(ch not in '0123456789abcdef' for ch in digest)
            or passage.get('locator') != f'{source}#chars={start}:{end}'
            or passage.get('adverse_candidate') is not False):
        return copy.deepcopy(passage)
    compact = copy.deepcopy(passage)
    compact.pop('quote')
    compact.update(quote_displayed=False, protected_quote_spans=decisive_spans(quote, source, start))
    return compact if estimate(encoded(compact)) < estimate(encoded(passage)) else copy.deepcopy(passage)


def select(run_id, rows, working, evidence_memory, valid_page, *, estimate=None, target=40000):
    from .context_compiler import source_page, page_key
    from .compression_safety import compact_card
    from .context_compiler import _terms
    from .compression_safety import protected
    estimate = estimate or (lambda value: len(value.encode()) // 3 + 1)
    terms = _terms(working.get('query', ''))
    candidates = {v['record_sha256']: v for rid,v in working['views'].items() if rid in working['consumed']}
    ranked = sorted(candidates, key=lambda key: (-len(terms & _terms(encoded(candidates[key]['record']))), key))
    guards = {key: guard_view(value) for key,value in candidates.items()}
    related_limit = working.get('_detail_target', int(target * .35))
    remaining = related_limit
    selected = set()
    def adverse(value):
        if isinstance(value, list):
            return any(adverse(item) for item in value)
        return isinstance(value, dict) and (protected(value) or any(adverse(v) for v in value.values()))
    # Reserve an independent relevance stratum for limitations/counterevidence.
    # All exact adverse fields remain in the guard views, even if the full record
    # cannot fit. Optional details may borrow unused slots; facts never get cut.
    counter_ranked = [key for key in ranked if adverse(candidates[key]['record'])]
    order = list(dict.fromkeys([*counter_ranked[:2], *ranked]))
    for key in order:
        cost = estimate(encoded(candidates[key]))
        if cost <= estimate(encoded(guards[key])):
            # Keeping the full record is both smaller and more informative than
            # its guarded directory. It costs no optional-detail allocation.
            selected.add(key)
        elif cost <= remaining and len(selected) < 6:
            selected.add(key)
            remaining -= cost
    working['selection_budget'] = {'relevant_details_target': related_limit,
        'relevant_details_used': related_limit - remaining,
        'counterevidence_target': int(target * .15),
        'counterevidence_candidates': len(counter_ranked),
        'counterevidence_used': sum(estimate(encoded(guards[key])) for key in candidates if key not in selected),
        'policy': 'Selected records preserve exact findings, conditions, adverse fields and unknown extensions. Unselected records retain an exact counterevidence inventory and archive pointer; they provide no unseen positive support. Slot targets are advisory; only configured/model input capacity is a hard limit.'}
    fresh_pages = {page_key(row) for row in rows if isinstance(row, dict)
        and receipt_id(run_id, row) not in working['consumed'] and source_page(row, valid_page)}
    packed, seen, pages_seen, views_seen, actions = [], set(), set(), set(), []
    for index, row in enumerate(rows):
        rid = receipt_id(run_id, row)
        if rid in seen:
            actions.append({'receipt_index': index, 'action': 'duplicate_receipt', 'receipt_id': rid})
            continue
        seen.add(rid)
        completed = working.get('completed_notes', {})
        value, call = row.get('result'), row.get('call', {})
        relevant = []
        if rid in working['consumed'] and isinstance(value, dict) and not value.get('error'):
            source = value.get('source_id')
            if source and call.get('name') in {'sources.read', 'pdf.read', 'sources.fetch', 'sources.open_reference'}:
                relevant = [note for note in completed.values()
                            if note['source_id'] == source and note['text_sha256'] == value.get('text_sha256')]
            elif call.get('name') == 'literature.record_notes' and isinstance(value.get('notes'), list):
                ids = [note.get('id') for note in value['notes'] if isinstance(note, dict)]
                if ids and all(nid in completed for nid in ids):
                    relevant = [completed[nid] for nid in ids]
            elif call.get('name') == 'literature.record_protocol' and value.get('note_id') in completed:
                relevant = [completed[value['note_id']]]
        if relevant:
            packed.append({'call': {'name': call.get('name'), 'arguments_archived': True}, 'result': {
                'completed_reading_directory': True, 'notes': copy.deepcopy(relevant),
                **{key: copy.deepcopy(value[key]) for key in ('id', 'protocol_sha256') if key in value},
                'receipt_id': rid, 'receipt_sha256': sha(row), 'original_call_sha256': sha(call),
                'expand': {'tool': 'context.expand', 'arguments': {'receipt_id': rid, 'expected_sha256': sha(row)}},
                'primary_text_displayed': False, 'new_reading_credit': False, 'scientific_claims_verified': False,
                'policy': 'Completed and saved literature: index only, per user policy. Original notes, submitted arguments '
                    'and source receipts remain immutable. Expand before a new judgment requiring their details. '
                    'A historical save is not current source validation, scientific approval or a new reading receipt.'}})
            actions.append({'receipt_index': index, 'action': 'completed_reading_index', 'receipt_id': rid})
            continue
        if (rid in working['consumed'] and isinstance(row.get('result'), dict)
                and row.get('call', {}).get('name') == 'evidence.search'
                and row['result'].get('primary_source_reading') is False):
            value = copy.deepcopy(row['result'])
            if isinstance(value.get('passages'), list):
                value['passages'] = [retrieval_directory(passage, estimate) for passage in value['passages']]
            value.update(receipt_id=rid, receipt_sha256=sha(row),
                expand={'tool': 'context.expand', 'arguments': {'receipt_id': rid, 'expected_sha256': sha(row)}},
                policy='Committed retrieval navigation only; undisplayed quote text is archived. Exact measured clauses and qualifiers remain; adverse candidates stay exact. Expand before interpreting undisplayed evidence. No reading credit is granted.')
            packed.append({**copy.deepcopy(row), 'result': value})
            actions.append({'receipt_index': index, 'action': 'committed_retrieval_directory', 'receipt_id': rid})
            continue
        projected = source_page(row, valid_page) if isinstance(row, dict) else None
        if projected:
            identity = page_key(row)
            if identity in pages_seen:
                actions.append({'receipt_index': index, 'action': 'shared_visible_source_page', 'receipt_id': rid})
                continue
            pages_seen.add(identity)
        card = (evidence_memory or {}).get(page_key(row)) if projected else None
        if card and rid in working['consumed'] and page_key(row) not in fresh_pages:
            try:
                compact = compact_card(card, projected['result'])
                card_view = copy.deepcopy(projected)
                result = card_view['result']
                result.pop('text')
                result.update(evidence_card=compact, primary_text_displayed=False)
                if estimate(encoded(card_view)) < estimate(encoded(projected)):
                    packed.append(card_view)
                    actions.append({'receipt_index': index, 'action': 'committed_evidence_card', 'receipt_id': rid})
                    continue
            except (ValueError, KeyError, TypeError):
                pass
        view = working['views'].get(rid) if rid in working['consumed'] else None
        if view:
            key = view['record_sha256']
            if key in views_seen:
                actions.append({'receipt_index': index, 'action': 'shared_structured_record', 'receipt_id': rid})
                continue
            views_seen.add(key)
            if key not in selected:
                view = guards[key]
            packed.append({'call': copy.deepcopy(row['call']), 'result': {
                **copy.deepcopy(view), 'receipt_id': rid, 'receipt_sha256': sha(row),
                'expand': {'tool': 'context.expand', 'arguments': {'receipt_id': rid, 'expected_sha256': sha(row)}}}})
            actions.append({'receipt_index': index, 'action': 'structured_record' if key in selected else 'committed_fact_record', 'receipt_id': rid})
            continue
        # A page without a validated record stays visible even if the model saw it.
        packed.append(copy.deepcopy(row))
    return packed, actions


async def expand(harness, run_id, role, args):
    from .history_directory import read as read_history
    historical = read_history(harness.engine.store, run_id, role, args)
    if historical is not None:
        return historical
    rid = args.get('receipt_id')
    archive = harness.engine.store.get('execution_archives', rid)
    if archive:
        from .execution_archive import load
        payload = load(harness.engine.store, run_id, role, rid, args.get('expected_sha256'))
        if 'path' in args:
            pointer = args['path']
            if not isinstance(pointer, str) or not pointer.startswith('/'):
                raise ValueError('Context path must be a JSON pointer')
            selected = payload.get('result')
            for part in (p.replace('~1', '/').replace('~0', '~') for p in pointer[1:].split('/')):
                if isinstance(selected, dict) and part in selected:
                    selected = selected[part]
                elif isinstance(selected, list) and part.isdecimal() and str(int(part)) == part and int(part) < len(selected):
                    selected = selected[int(part)]
                else:
                    raise ValueError('Context path does not exist')
            text = encoded(selected)
            offset, limit = args.get('offset', 0), args.get('limit', 4000)
            if type(offset) is not int or not 0 <= offset <= len(text) or type(limit) is not int or not 1 <= limit <= 6000:
                raise ValueError('Context selection offset/limit is invalid')
            end = min(len(text), offset + limit)
            return {'receipt_id': rid, 'receipt_sha256': archive['sha256'], 'json_pointer': pointer,
                'selection_sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text[offset:end],
                'offset': offset, 'total_chars': len(text), 'next_offset': end if end < len(text) else None,
                'truncated': end < len(text), 'untrusted': True, 'primary_source_reading': False,
                'scientific_claims_verified': False, 'provenance': 'exact_execution_archive_selection'}
        if 'offset' in args or 'limit' in args:
            raise ValueError('Context selection pagination requires a JSON path')
        return {'receipt_id': rid, 'receipt_sha256': archive['sha256'],
            'original_tool': payload.get('call', {}).get('name'), 'result': payload.get('result'),
            'untrusted': True, 'primary_source_reading': False, 'scientific_claims_verified': False,
            'provenance': 'exact_execution_archive_expansion', 'redacted': archive.get('redacted', False)}
    record = harness.engine.store.get('context_receipts', rid)
    if (not record or record.get('run_id') != run_id or sha(record.get('receipt')) != record.get('sha256')
            or args.get('expected_sha256') != record.get('sha256')):
        raise ValueError('Context receipt version or ownership mismatch')
    commits = [c for c in harness.engine.store.list('context_commits', run_id)
               if c.get('role') == role and rid in c.get('payload', {}).get('receipt_ids', [])
               and c.get('sha256') == sha(c.get('payload'))]
    if not commits:
        raise ValueError('Receipt was not committed for this role')
    row = record['receipt']
    if 'path' in args:
        # A JSON pointer can inspect a decisive saved field without reloading an
        # entire page/bundle. This is historical navigation, never reading credit.
        pointer = args['path']
        if not isinstance(pointer, str) or not pointer.startswith('/'):
            raise ValueError('Context path must be a JSON pointer')
        parent = _parent(harness.engine.store, run_id, row)
        selected = parent[0] if parent else row.get('result')
        for part in (p.replace('~1', '/').replace('~0', '~') for p in pointer[1:].split('/')):
            if isinstance(selected, dict) and part in selected:
                selected = selected[part]
            elif isinstance(selected, list) and part.isdecimal() and str(int(part)) == part and int(part) < len(selected):
                selected = selected[int(part)]
            else:
                raise ValueError('Context path does not exist')
        text = encoded(selected)
        offset, limit = args.get('offset', 0), args.get('limit', 4000)
        if type(offset) is not int or not 0 <= offset <= len(text) or type(limit) is not int or not 1 <= limit <= 6000:
            raise ValueError('Context selection offset/limit is invalid')
        end = min(len(text), offset + limit)
        original_result = row.get('result')
        return {'receipt_id': rid, 'receipt_sha256': record['sha256'],
            'document_sha256': original_result.get('sha256') if isinstance(original_result, dict) else None, 'json_pointer': pointer,
            'selection_sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text[offset:end],
            'offset': offset, 'total_chars': len(text), 'next_offset': end if end < len(text) else None,
            'truncated': end < len(text), 'untrusted': True, 'primary_source_reading': False,
            'scientific_claims_verified': False, 'provenance': 'exact_committed_context_selection'}
    if 'offset' in args or 'limit' in args:
        raise ValueError('Context selection pagination requires a JSON path')
    if row.get('call', {}).get('name') in {'sources.read', 'pdf.read'}:
        # Re-open through existing role/reading gates and produce a fresh audit.
        original_args = copy.deepcopy(row['call']['arguments'])
        original_args['expected_sha256'] = row['result']['text_sha256']
        return await harness.execute_controller_tool(run_id, role, row['call']['name'], original_args,
            purpose=f'context-reopen:{rid}:{record["sha256"]}')
    return {'original_tool': row.get('call', {}).get('name'),
            'original_call': copy.deepcopy(row.get('call')),
            'receipt_id': rid, 'receipt_sha256': record['sha256'],
            'result': copy.deepcopy(row.get('result')), 'untrusted': True,
            'provenance': 'exact_committed_tool_receipt_expansion', 'primary_source_reading': False}
