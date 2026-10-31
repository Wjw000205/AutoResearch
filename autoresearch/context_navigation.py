"""Small navigation views; original source metadata and research evidence persist."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .compression_safety import pinned


def links_hash(links):
    return hashlib.sha256(json.dumps(links, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def source_directory_entry(row, run_id):
    """Externalize only plain, explicitly uninspected navigation links."""
    if (not isinstance(row, dict) or not isinstance(row.get('source_id'), str)
            or not row['source_id'].startswith(run_id + ':source:')
            or row.get('artifact_id') != row['source_id'] or row.get('untrusted') is not True
            or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('text_sha256', '')))):
        return copy.deepcopy(row)
    links = row.get('source_links')
    if (not isinstance(links, list) or not links or any(not isinstance(link, dict)
            or set(link) != {'url', 'verified', 'retrieved', 'relation'}
            or not isinstance(link['url'], str) or link['verified'] is not False
            or link['retrieved'] is not False or link['relation'] != 'link_in_retrieved_source' for link in links)):
        return copy.deepcopy(row)
    result = {k: copy.deepcopy(v) for k, v in row.items() if k != 'source_links'}
    result['navigation'] = {'count': len(links), 'displayed': False,
        'read_tool': 'sources.links', 'links_sha256': links_hash(links)}
    return result


def reading_catalog(view):
    """A reading assignment gets a directory; it can expand precise saved notes.

    This is not used for novelty/consensus judgments. All saved adverse facts,
    current analysis, gates and primary read receipts remain displayed.
    """
    result = copy.deepcopy(view)
    doc = result['document']
    analysis = doc.get('analysis', {})
    focus = set(analysis.get('paper_ids', [])) if isinstance(analysis, dict) else set()
    if focus:
        # The explicit current reading plan takes precedence over generic
        # lexical hits from unrelated earlier topics in the same project.
        outside = [n for n in doc['notes'] if n.get('paper_id') not in focus and not pinned(n)]
        if outside:
            doc['outside_current_reading'] = [{k: n[k] for k in ('paper_id', 'id', 'archive_pointer') if k in n}
                for n in outside]
            doc['notes'] = [n for n in doc['notes'] if n not in outside]
            doc.setdefault('selection', {})['reading_directory_notes'] = len(doc['notes'])
            doc['selection']['outside_current_reading_count'] = len(outside)
    for note in doc['notes']:
        if not isinstance(note, dict) or not note.get('archive_pointer'):
            continue
        if pinned(note):
            note.update(detail_level='protected_evidence', historical_results_displayed='results' in note)
            continue
        archived = list(note.get('archived_detail_fields', []))
        for field in ('research_question', 'method', 'datasets', 'baselines', 'relevance', 'results', 'source_anchors'):
            if field in note:
                if field not in archived:
                    archived.append(field)
                note.pop(field)
        note.update(detail_level='reading_directory', archived_detail_fields=archived,
            historical_results_displayed=False)
    coverage = doc.get('coverage')
    if isinstance(coverage, dict) and 'queries' in coverage:
        # Keep errors, gaps, exhaustiveness flags and all other coverage fields.
        coverage.pop('queries')
        coverage['query_details_archive_pointer'] = '/coverage/queries'
    result['policy'] = ('Reading directory only: summaries, limitations, missing information and verification flags '
        'are copied exactly. Pinned negative findings keep their methods, measurements and anchors; '
        'other historical methods, measurements and locators are not displayed. '
        'Use evidence.read(artifact_id, expected_sha256=sha256, path=archive_pointer) to inspect the saved note, '
        'then sources.read/pdf.read for necessary primary passages. Do not reconstruct unseen measurements '
        'or infer support, novelty, coverage or experiment approval from this directory. '
        'outside_current_reading lists historical notes outside analysis.paper_ids; they are archived, not rejected or resolved.')
    return result
