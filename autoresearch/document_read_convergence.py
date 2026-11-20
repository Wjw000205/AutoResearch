"""Detect repeated evidence retrieval without rewriting a read ledger."""
from __future__ import annotations

import copy
import hashlib
import json

PROTOCOL = 'document_read_convergence_protocol'
VERSION = 1
WINDOW = 12


class ReadingStalled(RuntimeError):
    def __init__(self, handoff_id, diagnostic):
        self.handoff_id, self.diagnostic = handoff_id, diagnostic
        super().__init__('Document reading has no new evidence or saved output; Harness clarification required')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False).encode('utf-8')).hexdigest()


def analyze(receipts, *, window=WINDOW, observation_documents=()):
    if type(window) is not int or window < 2:
        raise ValueError('Invalid read convergence window')
    ranges, saved, idle, reads, repeated, added = {}, set(), 0, 0, 0, 0
    recent = []
    for position, row in enumerate(receipts):
        call, result = row.get('call', {}), row.get('result', {})
        if not isinstance(result, dict):
            continue
        if result.get('saved') is True and isinstance(result.get('spec_hash'), str):
            key = result['spec_hash']
            if key not in saved:
                saved.add(key)
                idle = 0
            continue
        name = call.get('name')
        if name not in {'documents.read', 'documents.search'}:
            continue
        reads += 1
        novel = 0
        if (name == 'documents.read' and not result.get('error')
                and isinstance(result.get('text'), str) and result.get('document_id') and result.get('sha256')
                and result.get('document_id') not in observation_documents):
            start, end = result.get('char_start'), result.get('char_end')
            if type(start) is not int or type(end) is not int or end - start != len(result['text']) or start < 0:
                raise ValueError('Read convergence requires exact character ranges')
            key = (result['document_id'], result['sha256'])
            spans = ranges.setdefault(key, [])
            remaining = [(start, end)]
            for a, b in spans:
                remaining = [p for x, y in remaining for p in ((x, min(y, a)), (max(x, b), y)) if p[0] < p[1]]
            novel = sum(b - a for a, b in remaining)
            merged = []
            for a, b in sorted([*spans, (start, end)]):
                if merged and a <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                else:
                    merged.append((a, b))
            ranges[key] = merged
        added += novel
        repeated += int(novel == 0)
        idle = 0 if novel else idle + 1
        recent = [*recent, {'ledger_position': position, 'new_characters': novel,
            'document_id': result.get('document_id'), 'sha256': result.get('sha256'),
            'char_start': result.get('char_start'), 'char_end': result.get('char_end')}][-window:]
    return {'version': VERSION, 'reads': reads, 'unique_characters': added,
        'reads_without_new_material': repeated, 'consecutive_reads_without_progress': idle,
        'stalled': idle >= window, 'window': window, 'recent': recent,
        'ledger_sha256': digest(receipts), 'scientific_approval': False}


def diagnose(engine, run_id, role, key, progress, context):
    """One authenticated observation classification for guard and recovery."""
    from . import document_index, document_working_notes as notes
    observations = set()
    inherited_owners = {}
    inherited = [row for row in progress.get('tool_results', []) if row.get('predecessor_handoff_id')]
    if inherited:
        from .document_recovery_receipts import inherited_rows
        owner, original = inherited_rows(engine, run_id, role, key, inherited)
        inherited_owners = {row.get('result', {}).get('document_id'): owner for row in original
                            if isinstance(row.get('result'), dict)}
    for did in {r.get('result', {}).get('document_id') for r in progress.get('tool_results', [])
                if isinstance(r.get('result'), dict)} - {None}:
        manifest = engine.store.get('memory_documents', did, {})
        if manifest.get('document_kind') != 'document_working_notes':
            continue
        directory = document_index.directory(engine, run_id, did, manifest['text_sha256'])
        _, text = document_index.load(engine, run_id, did, directory['sha256'])
        payload = json.loads(text.split('```json\n', 1)[1].rsplit('\n```\n', 1)[0])
        notes._checked(engine, run_id, role, inherited_owners.get(did, key), {'document_working_notes': {
            'payload': payload, 'sha256': notes.sha(payload), 'document': directory}})
        observations.add(did)
    return analyze(progress.get('tool_results', []), observation_documents=observations)


def guard(h, run_id, role, key, progress, context):
    if context.get(PROTOCOL) != VERSION:
        return
    frozen = h.engine.store.get('handoff_inputs', key, {})
    if frozen.get('run_id') != run_id or frozen.get('context', {}).get(PROTOCOL) != VERSION:
        raise ValueError('Read convergence marker is not frozen by this Harness assignment')
    diagnostic = diagnose(h.engine, run_id, role, key, progress, context)
    if diagnostic['stalled']:
        raise ReadingStalled(key, copy.deepcopy(diagnostic))
