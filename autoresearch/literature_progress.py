"""Read-only material progress for bounded literature-reading recovery.

Only task-owned, locally revalidated evidence contributes. Download attempts,
source IDs, timestamps, failure messages and duplicate reads are deliberately
absent so they cannot authorize another paid reading assignment by themselves.
This projection establishes available/inspected text, not scientific validity.
"""
from __future__ import annotations

from .lab import source_text_issue
from .literature_notes import READING_STATUSES, _read_receipts, valid_paper_ids
from .literature_reading import _run_papers
from .storage import RunDeletedError


def _require_run(store, run_id):
    if store.get('run_deletions', run_id):
        raise RunDeletedError('Research is being deleted or has been deleted')
    if not store.get('runs', run_id):
        raise ValueError('Research not found')


def _merged_ranges(rows):
    merged = []
    for start, end in sorted(set(rows)):
        if merged and start <= merged[-1]['end']:
            merged[-1]['end'] = max(merged[-1]['end'], end)
        else:
            merged.append({'start': start, 'end': end})
    return merged


def reading_material(engine, run_id) -> dict:
    """Return stable available text hashes, inspected ranges and valid note IDs.

    Historical successful acquisitions remain eligible after a failed refresh.
    Every retained source is read through the scoped, hash-checking document
    loader. Malformed/missing evidence cannot create progress; deletion and
    cancellation are not swallowed. No records or files are written.
    """
    store = engine.store
    _require_run(store, run_id)
    usable, ranges, documents = set(), {}, {}
    for paper_id in sorted(_run_papers(store, run_id)):
        evidence = store.get('run_evidence', f'{run_id}:{paper_id}')
        if (not isinstance(evidence, dict) or evidence.get('run_id') != run_id
                or not isinstance(evidence.get('paper'), dict)
                or evidence['paper'].get('id') != paper_id):
            continue
        candidates = [evidence.get('reading')]
        history = evidence.get('reading_history', [])
        if isinstance(history, list):
            candidates.extend(item.get('result') for item in history if isinstance(item, dict))
        for row in candidates:
            if (not isinstance(row, dict) or row.get('paper_id') != paper_id
                    or row.get('identity_verified') is not True
                    or row.get('retrieval_status') not in READING_STATUSES
                    or not isinstance(row.get('source_id'), str)):
                continue
            source_id = row['source_id']
            try:
                if source_id not in documents:
                    document = engine.sources.document(run_id, source_id)
                    documents[source_id] = (document if isinstance(document.get('text'), str)
                        and document['text'].strip() and not source_text_issue(document) else None)
                document = documents[source_id]
                if (not document or document.get('source_id') != source_id
                        or document.get('run_id') != run_id
                        or row.get('text_sha256') != document.get('text_sha256')
                        or row.get('content_sha256') != document.get('content_sha256')):
                    continue
                key = (paper_id, document['text_sha256'])
                usable.add(key)
                receipts = _read_receipts(store, run_id, document, allow_empty=True)
                ranges.setdefault(key, []).extend((receipt['start'], receipt['end']) for receipt in receipts)
            except RunDeletedError:
                raise
            except (ValueError, KeyError, TypeError, OSError):
                # Changed/missing local artifacts do not become new material.
                # Failed acquisitions remain available in their original log.
                continue
    noted = sorted(valid_paper_ids(engine, run_id))
    _require_run(store, run_id)
    return {
        'usable_sources': [{'paper_id': paper_id, 'text_sha256': text_hash}
                           for paper_id, text_hash in sorted(usable)],
        'read_ranges': [{'paper_id': paper_id, 'text_sha256': text_hash,
                         'ranges': _merged_ranges(rows)}
                        for (paper_id, text_hash), rows in sorted(ranges.items()) if rows],
        'noted_paper_ids': noted,
    }
