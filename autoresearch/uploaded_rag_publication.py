"""Publish a validated local supplement without inventing upload reading credit."""
from __future__ import annotations

import copy

from .paper_identity import same_paper


def reused_supplement(rag, run_id, paper, note, document, evidence):
    """The caller supplies live, validated uploaded_rag_evidence, never a flag.

    Keep the base paper's identity and any historical upload completion intact.
    A local passage note adds analysis; it does not complete a fresh full read.
    """
    version = evidence['rag_version_id']
    base = rag.load(version)  # Recheck the immutable snapshot at publication.
    if (note.get('run_id') != run_id or base != evidence['payload'] or evidence['paper_id'] != paper['id']
            or not same_paper(rag.store, paper, base['paper'])
            or note['text_sha256'] != document['text_sha256']
            or document['text_sha256'] != base['note']['text_sha256']
            or document.get('content_sha256') != base['document'].get('content_sha256')
            or document.get('rag_version_id') != version):
        raise ValueError('Uploaded supplement does not match its validated shared source snapshot')
    inherited = base['paper'].get('identity_kind') == 'uploaded_document'
    if inherited and base.get('uploaded_analysis_complete') is not True:
        raise ValueError('An incomplete uploaded analysis cannot authorize a supplement')
    result = {'paper': copy.deepcopy(base['paper']), 'uploaded_analysis_reuse': {
        'rag_version_id': version,
        'snapshot_sha256': rag.store.get('paper_rag_versions', version)['snapshot_sha256'],
        'origin_run_id': base['origin_run_id'], 'origin_note_id': base['origin_note_id'],
        'analysis_created_at': base['analysis_created_at'],
        'source_id': evidence['source_id'], 'text_sha256': document['text_sha256'],
        'content_sha256': document.get('content_sha256'),
        'uploaded_original_source_id': paper.get('uploaded_source_id'),
        'uploaded_original_content_sha256': paper.get('uploaded_content_sha256'),
        'new_reading_credit': False, 'scientific_claims_verified': False,
        'scope': 'Validated local passage supplement to an attributed shared analysis; no new full-document reading credit.'}}
    if inherited:
        result.update(uploaded_analysis_complete=True,
            uploaded_reading_completion=copy.deepcopy(base['uploaded_reading_completion']),
            uploaded_reading_completion_inherited=True,
            bibliographic_identity_verified=base.get('bibliographic_identity_verified') is True)
    return result
