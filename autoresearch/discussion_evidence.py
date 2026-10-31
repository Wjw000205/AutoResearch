"""Resolve retrieved task records without promoting summaries to source evidence.

The paid decision stays immutable. Only exact, still-valid records returned by
this handoff's tools can move to record_references in the derived delivery.
Unknown IDs, foreign records and changed bytes continue to block delivery.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path


GATE = 'Discussion delivery cited evidence outside the saved task index'


class DiscussionEvidenceError(ValueError):
    def __init__(self, identifiers):
        super().__init__(GATE)
        self.identifiers = sorted(set(identifiers))


def _paper_reference(engine, run_id, identifier, snapshot):
    from .paper_identity import resolve, same_paper, task_paper
    from .paper_bindings import shared_alias, valid_binding
    papers = snapshot.get('verified_papers', [])
    owned = task_paper(engine.store, run_id, identifier)
    if owned and any(same_paper(engine.store, owned, paper) for paper in papers):
        return {'id': identifier, 'kind': 'canonical_paper_alias', 'paper_id': owned['id'],
                'canonical_paper_id': resolve(engine.store, owned), 'primary_source_reading': False}
    physical_id = shared_alias(engine.store, run_id, identifier) or identifier
    binding = valid_binding(engine.store, run_id, physical_id)
    if not binding:
        return None
    owned = task_paper(engine.store, run_id, binding['paper_id'])
    if not owned or not any(same_paper(engine.store, owned, paper) for paper in papers):
        return None
    try:
        document = engine.sources.document(run_id, identifier)
    except (ValueError, OSError, KeyError, TypeError):
        return None
    if any(document.get(k) != binding[k] for k in ('text_sha256', 'content_sha256')):
        return None
    return {'id': identifier, 'kind': 'canonical_paper_source', 'paper_id': owned['id'],
        'canonical_paper_id': binding['canonical_paper_id'], 'source_alias_id': physical_id,
        'text_sha256': binding['text_sha256'], 'content_sha256': binding['content_sha256'],
        'primary_source_reading': False}


def _record_reference(engine, run_id, identifier, tools):
    for index, tool in enumerate(tools):
        call, result = tool.get('call', {}), tool.get('result')
        if call.get('name') == 'memory.search' and isinstance(result, list):
            row = engine.store.get('memory', identifier)
            if not row or row.get('id') != identifier or row.get('run_id') != run_id:
                continue
            for hit in result:
                if not isinstance(hit, dict) or hit.get('id') != identifier:
                    continue
                fields = ('id', 'run_id', 'role', 'document_id', 'text', 'metadata', 'created_at')
                if (not isinstance(row.get('text'), str) or not row['text'].strip()
                        or any(hit.get(k) != row.get(k) for k in fields)):
                    continue
                return {'id': identifier, 'kind': 'task_memory',
                    'sha256': hashlib.sha256(row['text'].encode()).hexdigest(),
                    'document_id': row.get('document_id'), 'metadata': copy.deepcopy(row.get('metadata', {})),
                    'tool_result_index': index}
        elif (call.get('name') == 'artifacts.read' and isinstance(result, dict)
              and not result.get('error') and result.get('artifact_id') == identifier):
            args = call.get('arguments', {})
            artifact = engine.store.get('artifacts', identifier)
            if (args.get('id') != identifier or not artifact or artifact.get('id') != identifier
                    or artifact.get('run_id') != run_id):
                continue
            try:
                path = Path(artifact['path']).resolve()
                if not path.is_relative_to(engine.run_dir(run_id).resolve()):
                    continue
                text = path.read_text(encoding='utf-8')
            except (OSError, ValueError, KeyError):
                continue
            sha = hashlib.sha256(text.encode()).hexdigest()
            offset, limit = args.get('offset', 0), args.get('limit', 16000)
            if (type(offset) is not int or type(limit) is not int
                    or offset < 0 or not 1 <= limit <= 16000 or offset >= len(text)
                    or sha != result.get('sha256') or sha != artifact.get('sha256')
                    or args.get('expected_sha256', sha).lower() != sha
                    or result.get('offset') != offset or result.get('total_chars') != len(text)
                    or result.get('text') != text[offset:offset + limit]):
                continue
            return {'id': identifier, 'kind': 'task_artifact', 'sha256': sha,
                'offset': offset, 'end': min(offset + limit, len(text)),
                'total_chars': len(text), 'tool_result_index': index}
    return None


def _frozen_request(engine, run_id, handoff_id, progress):
    handoff = engine.store.get('handoffs', handoff_id, {})
    slot = 'finalization' if progress.get('mode') == 'final_only' else 'normal_request'
    prepared = progress.get(slot, {}).get('prepared', {})
    if (handoff.get('id') != handoff_id or handoff.get('run_id') != run_id
            or not handoff_id.startswith(run_id + ':')
            or set(('role', 'task', 'context', 'schema')) - prepared.keys()
            or handoff.get('to_role') != prepared['role']):
        return None
    receipts = prepared['context'].get('tool_results', [])
    if not receipts or any(row not in progress.get('tool_results', []) for row in receipts):
        return None
    return prepared


def _catalog_reference(engine, run_id, identifier, prepared):
    """A bounded catalog selection is navigation, even when fully delivered."""
    from .context_catalog import read
    artifact = engine.store.get('artifacts', identifier, {})
    if (artifact.get('id') != identifier or artifact.get('run_id') != run_id
            or artifact.get('kind') != 'literature_index'):
        return None
    for index, tool in enumerate(prepared['context']['tool_results']):
        call, result = tool.get('call', {}), tool.get('result')
        args = call.get('arguments', {})
        if (call.get('name') != 'evidence.read' or not isinstance(result, dict)
                or result.get('error') or args.get('artifact_id') != identifier
                or args.get('expected_sha256') != artifact.get('sha256')):
            continue
        try:
            expected = read(engine, run_id, args)
        except (ValueError, TypeError, KeyError, OSError):
            continue
        if expected != result:
            continue
        return {'id': identifier, 'kind': 'task_catalog_selection',
            'sha256': expected['source_sha256'], 'selection_sha256': expected['selection_sha256'],
            'page_sha256': hashlib.sha256(expected['text'].encode()).hexdigest(),
            'json_pointer': expected['json_pointer'], 'offset': expected['offset'],
            'end': expected['offset'] + len(expected['text']), 'total_chars': expected['total_chars'],
            'primary_source_reading': False, 'tool_result_index': index}
    return None


def _supplied_context(engine, run_id, prepared):
    """Read the exact compiled input for this frozen operation, not a new view."""
    from .intelligence import Intelligence
    from .storage import redact
    operation = Intelligence._operation_identity(None, **prepared)
    for snapshot in reversed(engine.store.list('context_snapshots', run_id)):
        if snapshot.get('run_id') != run_id or snapshot.get('role') != prepared['role']:
            continue
        original = engine.store.get('transcripts', snapshot.get('original_transcript_id'), {})
        if (original.get('run_id') != run_id or original.get('role') != prepared['role']
                or original.get('operation_id') != operation
                or original.get('task') != redact(prepared['task'])
                or original.get('context') != redact(prepared['context'])):
            continue
        active = snapshot['active_context']
        def expand(value):
            if isinstance(value, dict) and set(value) == {'$text'}:
                return active['shared_context_text'][value['$text']]
            if isinstance(value, dict) and set(value) == {'$catalog_record'}:
                return expand(active['shared_context_catalog_records'][value['$catalog_record']])
            if isinstance(value, dict):
                return {k: expand(v) for k, v in value.items()}
            if isinstance(value, list):
                return [expand(v) for v in value]
            return value
        supplied = expand(active)
        if supplied.get('task') == redact(prepared['task']):
            return supplied, snapshot['id']
    return None, None


def _card_references(engine, run_id, prepared, identifiers):
    from .compression_safety import compact_card, protect_card
    from .context_compiler import fingerprint, page_key, source_page
    from .context_reduction import scope_for, valid_saved
    from .intelligence import _artifact_page
    supplied, snapshot_id = _supplied_context(engine, run_id, prepared)
    if not supplied or scope_for(supplied) != scope_for(prepared):
        return {}
    tools = prepared['context']['tool_results']
    raw_pages = {page_key(row): (index, row['result']) for index, row in enumerate(tools)
                 if source_page(row, _artifact_page) is not None}
    references, validated_jobs = {}, {}
    for row in supplied.get('context', {}).get('tool_results', []):
        value = row.get('result')
        card = value.get('evidence_card') if isinstance(value, dict) else None
        if not isinstance(card, dict):
            continue
        wanted = [f for f in card.get('findings', []) if f.get('evidence_id') in identifiers]
        if not wanted:
            continue
        key = card.get('extraction_id')
        job = engine.store.get('context_evidence', key, {})
        if (job.get('run_id') != run_id or job.get('id') != key
                or job.get('prepared', {}).get('context', {}).get('extraction_scope') != scope_for(prepared)):
            continue
        if key not in validated_jobs:
            validated_jobs[key] = valid_saved(job, run_id)
        cards = validated_jobs[key]
        if not cards or cards != job.get('cards'):
            continue
        for page_id, expected in cards.items():
            if page_id not in raw_pages:
                continue
            index, raw = raw_pages[page_id]
            if card not in (protect_card(expected, raw), compact_card(expected, raw)):
                continue
            document = engine.sources.document(run_id, card['source_id'])
            start = raw['offset']
            if (document['text_sha256'] != raw['text_sha256']
                    or raw['total_chars'] != len(document['text'])
                    or document['text'][start:start + len(raw['text'])] != raw['text']):
                continue
            for finding in wanted:
                references[finding['evidence_id']] = {
                    'id': finding['evidence_id'], 'kind': 'task_evidence_card',
                    'sha256': fingerprint(finding), 'source_id': card['source_id'],
                    'text_sha256': card['text_sha256'], 'extraction_id': key,
                    'compiled_snapshot_id': snapshot_id, 'tool_result_index': index,
                    'finding': copy.deepcopy(finding), 'limitations': copy.deepcopy(card['limitations']),
                    'provenance': card['provenance'], 'complete_paper_reading': False}
    return references


def _visual_reference(engine, run_id, identifier, prepared):
    from .context_compiler import fingerprint
    from .image_inputs import resolve
    from .paper_media import VISUAL_TASK, VISUAL_SCHEMA, validate_visual
    saved = engine.store.get('paper_visual', identifier, {})
    if (saved.get('id') != identifier or saved.get('run_id') != run_id
            or not identifier.startswith(run_id + ':visual:') or saved.get('status') != 'completed'
            or saved.get('scientific_claims_verified') is not False):
        return None
    public = {k: v for k, v in saved.items() if k != 'prepared'}
    for index, tool in enumerate(prepared['context']['tool_results']):
        if tool.get('call', {}).get('name') != 'pdf.analyze_figure' or tool.get('result') != public:
            continue
        args, rendered = tool['call'].get('arguments', {}), saved['rendered']
        if (args.get('source_id') != rendered['source_id'] or args.get('page') != rendered['page']
                or args.get('crop', [0, 0, 1, 1]) != rendered['crop']
                or args.get('expected_sha256') != rendered['text_sha256']
                or args.get('question') != saved['question'] or public.get('error')):
            continue
        request = saved['prepared']
        context = request['context']
        identity = identifier.rsplit(':visual:', 1)[1]
        if (request['role'] != prepared['role'] or request['task'] != VISUAL_TASK or request['schema'] != VISUAL_SCHEMA
                or context.get('_purpose') != 'paper_visual:' + identity
                or context.get('_image_inputs') != [rendered['image']]
                or context.get('page') != rendered or context.get('question') != saved['question']):
            continue
        validate_visual(saved['response'])
        if saved['response_sha256'] != fingerprint(saved['response']):
            continue
        # document() validates current task ownership, raw text and original bytes.
        document = engine.sources.document(run_id, rendered['source_id'])
        original = engine.store.get('artifacts', document.get('original_artifact_id'), {})
        if (document['text_sha256'] != rendered['text_sha256']
                or original.get('id') != rendered['original_artifact_id']
                or original.get('sha256') != rendered['original_sha256']):
            continue
        image = resolve(engine.store, run_id, {'_image_inputs': [rendered['image']]})[0]
        if any(rendered.get(k) != image[k] for k in ('width', 'height')):
            continue
        return {'id': identifier, 'kind': 'task_visual_analysis', 'sha256': saved['response_sha256'],
            'tool_result_index': index, 'rendered': copy.deepcopy(rendered), 'question': saved['question'],
            'response': copy.deepcopy(saved['response']), 'request_id': saved.get('request_id'),
            'provenance': saved.get('provenance'), 'coverage': saved['coverage']}
    return None


def resolve_delivery(engine, run_id, handoff_id, result, snapshot):
    from .engine import digest
    from jsonschema import ValidationError

    allowed = {p['id'] for p in snapshot.get('verified_papers', [])}
    allowed.update(p['source_id'] for p in snapshot.get('sources', []))
    allowed.update(p['id'] for p in snapshot.get('literature_notes', []))
    progress = engine.store.get('handoff_progress', handoff_id, {})
    tools = progress.get('tool_results', []) if handoff_id.startswith(run_id + ':') else []
    identifiers = {identifier for outcome in result['outcomes'] for identifier in outcome['evidence_ids']}
    prepared = _frozen_request(engine, run_id, handoff_id, progress)
    cards = {}
    if prepared and any(identifier.startswith('E-') for identifier in identifiers):
        try:
            cards = _card_references(engine, run_id, prepared, identifiers)
        except (ValueError, TypeError, KeyError, OSError, ValidationError):
            pass  # A corrupt binding cannot authorize a derived reference.
    derived, reclassified, invalid = copy.deepcopy(result), [], []
    for outcome_index, outcome in enumerate(derived['outcomes']):
        retained, records = [], []
        for citation_index, identifier in enumerate(outcome['evidence_ids']):
            if identifier in allowed:
                retained.append(identifier)
                continue
            reference = _record_reference(engine, run_id, identifier, tools) or copy.deepcopy(cards.get(identifier))
            if reference is None:
                reference = _paper_reference(engine, run_id, identifier, snapshot)
            if reference is None and prepared and identifier.startswith(run_id + ':literature/index/'):
                reference = _catalog_reference(engine, run_id, identifier, prepared)
            if reference is None and prepared and identifier.startswith(run_id + ':visual:'):
                try:
                    reference = _visual_reference(engine, run_id, identifier, prepared)
                except (ValueError, TypeError, KeyError, OSError, ValidationError):
                    pass
            if reference is None:
                invalid.append(identifier)
                continue
            reference.update(scientific_claims_verified=False, grants_reading_credit=False,
                scope='本任务已实际提供且可复核的记录或模型分析；保留推断、限制和原始来源，不授予完整阅读信用或科学结论。')
            records.append(reference)
            reclassified.append({'path': f'/outcomes/{outcome_index}/evidence_ids/{citation_index}',
                'original_id': identifier, **reference})
        outcome['evidence_ids'] = retained
        if records:
            outcome['record_references'] = records
    if invalid:
        raise DiscussionEvidenceError(invalid)
    audit = {'version': 1, 'policy': 'separate_retrieved_task_records',
        'handoff_id': handoff_id, 'snapshot_hash': snapshot.get('snapshot_hash'),
        'original_result_hash': digest(result), 'derived_result_hash': digest(derived),
        'record_references': reclassified, 'scientific_claims_verified': False,
        'grants_reading_credit': False,
        'scope': '仅重新分类已实际读取且仍可校验的本任务记录；原回复、设计建议和未完成缺口保留，独立共识与执行门禁继续适用。'}
    return derived, audit
